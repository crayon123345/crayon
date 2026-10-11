"""PR-5 FactStore seam core - the 9-step DELETION-WINS transaction (MODEL ONLY).

Contract: docs/memory/pr5-factstore.md (PR-5 contract v80). This module is an
UNWIRED synthetic disposable-SQLite MODEL. It composes the MERGED PR-1/PR-3/
PR-4 modules UNMODIFIED:
  PR-1: ports/memory/owner_counters.py (monotone per-owner epoch allocator)
  PR-3: ports/memory/capability.py   (evaluator, snapshot, typed errors)
  PR-4: ports/memory/envelope.py     (binding, claim, registry, issuers)

Boundary (contract sec 1): MODEL ONLY. No wiring, no real keys/content, no
migrations, no non-disposable DDL, no network, no external preflight. ONE
disposable SQLite connection (RECEIVER scope ONLY - the issuer harness's
fixture_store_conn is a SEPARATE connection outside this rule; NO cross-DB
atomicity). BEGIN IMMEDIATE is the serialization point and is NOT a Postgres
proof. PRODUCTION STUBS for every surface REFUSE UNCONDITIONALLY
(ProductionStubRefused / MODEL_ONLY). PRIVATE surface only: group writes and
extraction are deferred/refused in this PR. Public synthetic fixtures with
recorded provenance ("minted by fixture F"), never authority.
"""

from __future__ import annotations

import fcntl
import hashlib
import hmac as _hmac
import json
import os
import secrets
import sqlite3
import unicodedata
from dataclasses import dataclass
from enum import Enum
from typing import Optional, Protocol, Tuple

import capability as cap  # PR-3, UNMODIFIED (same import style as envelope.py)
import envelope as env    # PR-4, UNMODIFIED
import owner_counters     # PR-1, UNMODIFIED

BIGINT_MAX = 2**63 - 1

# Bounds (contract sec 3; code points unless BYTES stated)
KEY_MAX = 60
VALUE_MAX = 400
EXCERPT_MAX = 500
CODE_MAX = 64            # category/source/actor/ref codes
BOUNDED_STR_MAX = 128    # bounded_by/resolver/proof/evidence-ref/lineage
CURSOR_WIRE_LEN = 32     # ASCII
OP_ID_LEN = 32           # lowercase hex
HASH_LEN = 64            # lowercase hex
OLD_FIDS_MIN, OLD_FIDS_MAX = 1, 500
EVIDENCE_MAX_PER_OP = 64
LINEAGE_MAP_MAX_KEYS = 8
PAGE_MAX = 4096          # REPORT-PAGE bound ONLY - never an erasure bound
RETAINED_MAX = 9
UNKNOWN_COVERAGE_MAX = 8
LIVE_FACT_CAP = 500
LIST_LIMIT_MIN, LIST_LIMIT_MAX = 1, 200
FILTER_HASH_LEN = 16


# ---------------------------------------------------------------------------
# Validators (exact types; bool is not int; no coercion; NOTHING truncated)
# ---------------------------------------------------------------------------

def _req_int(value, minimum: int, maximum: int = BIGINT_MAX) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise cap.ModelInputError()
    return value


def _req_bool(value) -> bool:
    if type(value) is not bool:
        raise cap.ModelInputError()
    return value


def _req_enum(value, enum_cls):
    if type(value) is not enum_cls:
        raise cap.ModelInputError()
    return value


def _req_hex(value, length: int) -> str:
    if (type(value) is not str or len(value) != length
            or any(c not in "0123456789abcdef" for c in value)):
        raise cap.ModelInputError()
    return value


def _req_str(value, max_points: int, required: bool = True) -> str:
    """UTF-8 str, no surrogates, no control chars except tab/newline/CR,
    bounded in CODE POINTS; required strings refuse empty/whitespace-only.
    NOTHING is silently truncated."""
    if type(value) is not str:
        raise cap.ModelInputError()
    if len(value) > max_points:
        raise cap.ModelInputError()
    for ch in value:
        cp = ord(ch)
        if 0xD800 <= cp <= 0xDFFF:
            raise cap.ModelInputError()
        if cp < 0x20 and ch not in ("\t", "\n", "\r"):
            raise cap.ModelInputError()
        if cp == 0x7F:
            raise cap.ModelInputError()
    if required and not value.strip():
        raise cap.ModelInputError()
    return value


def _checked_add(a: int, b: int) -> int:
    _req_int(a, 0)
    _req_int(b, 0)
    out = a + b
    if out > BIGINT_MAX:
        raise cap.ModelInputError()
    return out


def _canon(obj) -> bytes:
    """Canonical JSON: sorted keys, no insignificant whitespace, UTF-8, no
    NaN/Infinity, duplicate keys impossible (dict input only)."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def _strict_loads(data: bytes):
    """Parser-safe decode: canonical-only JSON (sorted keys, no whitespace).
    Decodable-but-noncanonical refuses MALFORMED_INPUT (contract sec 4b)."""
    if type(data) is not bytes:
        raise cap.ModelInputError()
    try:
        text = data.decode("utf-8")
    except (UnicodeDecodeError, UnicodeError):
        raise cap.ModelInputError()
    try:
        obj = json.loads(text, parse_constant=_reject_constant,
                         parse_float=_reject_float,
                         object_pairs_hook=_no_dupes)
    except (json.JSONDecodeError, ValueError):
        raise cap.ModelInputError()
    if _canon(obj) != data:
        raise cap.ModelInputError()
    return obj


def _reject_constant(_value):
    raise cap.ModelInputError()


def _reject_float(_value):
    raise cap.ModelInputError()


def _no_dupes(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise cap.ModelInputError()
        out[key] = value
    return out


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _normalize_key(key: str) -> str:
    """LOCAL normalizer (contract sec 11): Unicode NFC, casefold, strip;
    empty-after-normalization refuses; oversize refuses. Distinct raw keys
    normalizing equal ARE a key match."""
    norm = unicodedata.normalize("NFC", key).casefold().strip()
    _req_str(norm, KEY_MAX)
    return norm


# ---------------------------------------------------------------------------
# q() - the NEW local SQL helper (contract sec 5). Same-transaction %s -> ?
# parameter translation, RETURNING emulation via SQLite >= 3.35 native
# RETURNING, BIGINT range checks, dict rows preserving explicit aliases.
# ---------------------------------------------------------------------------

class _Queryable:
    """The ONE disposable connection wrapper. Every store access goes through
    q(); also the injected `db` for the unmodified PR-1 allocator."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        if type(conn) is not sqlite3.Connection:
            raise cap.ModelInputError()
        self._conn = conn

    @property
    def raw(self) -> sqlite3.Connection:
        return self._conn

    def q(self, sql: str, params: tuple, mode: str):
        if type(sql) is not str or not sql:
            raise cap.ModelInputError()
        if type(params) is not tuple:
            raise cap.ModelInputError()
        for value in params:
            if type(value) is bool:
                raise cap.ModelInputError()
            if type(value) is int and not -BIGINT_MAX - 1 <= value <= BIGINT_MAX:
                raise cap.ModelInputError()
            if value is not None and type(value) not in (int, str, bytes):
                raise cap.ModelInputError()
        if mode not in ("one", "all", "exec"):
            raise cap.ModelInputError()
        translated = sql.replace("%s", "?")
        cursor = self._conn.execute(translated, params)
        if mode == "exec":
            return None
        rows = cursor.fetchall()
        names = [d[0] for d in cursor.description or ()]
        out = [dict(zip(names, row)) for row in rows]
        if mode == "one":
            return out[0] if out else None
        return out


# ---------------------------------------------------------------------------
# DDL (contract sec 5 - all tables/columns declared; FactStore-OWNED unless
# named PR-1/PR-3). Disposable fixture DB only.
# ---------------------------------------------------------------------------

_DDL = """
CREATE TABLE IF NOT EXISTS owners(
  uid INTEGER PRIMARY KEY, lifetime_id INTEGER NOT NULL,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS lifetime_state(
  uid INTEGER PRIMARY KEY, lifetime_id INTEGER NOT NULL,
  generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS issuer_state(
  uid INTEGER PRIMARY KEY, issuer_json TEXT NOT NULL, valid INTEGER NOT NULL,
  updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fixture_snapshot(
  uid INTEGER PRIMARY KEY, lifetime_id INTEGER NOT NULL,
  current_epoch INTEGER NOT NULL, grants_generation INTEGER NOT NULL,
  policy_revision INTEGER NOT NULL, decision_revision INTEGER NOT NULL,
  consent_generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fixture_grants(
  uid INTEGER NOT NULL, scope TEXT NOT NULL, granted_at INTEGER NOT NULL,
  expires_at INTEGER, per_op_required INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fixture_consents(
  uid INTEGER NOT NULL, operation_id TEXT NOT NULL,
  operation_class TEXT NOT NULL, scope TEXT NOT NULL,
  audience_id TEXT, permitted_information_ref TEXT NOT NULL,
  issued_at INTEGER NOT NULL, expires_at INTEGER NOT NULL,
  consent_generation INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fixture_policy(
  uid INTEGER PRIMARY KEY, extraction TEXT NOT NULL, sharing TEXT NOT NULL,
  group_write TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS fixture_envelope(
  uid INTEGER PRIMARY KEY, extraction TEXT NOT NULL, sharing TEXT NOT NULL,
  group_write TEXT NOT NULL, issued_at INTEGER NOT NULL,
  expires_at INTEGER, revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS epoch_state(
  uid INTEGER PRIMARY KEY, last_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS owner_counters(
  user_id INTEGER PRIMARY KEY, next_epoch INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS id_allocators(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  next_fact_id INTEGER NOT NULL, next_history_id INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id));
CREATE TABLE IF NOT EXISTS store_revisions(
  uid INTEGER PRIMARY KEY, store_revision INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS fact_lineage(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  created_op TEXT NOT NULL, last_op TEXT NOT NULL, updated_at INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fact_id));
CREATE TABLE IF NOT EXISTS lineage_ops(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  seq INTEGER NOT NULL, op_id TEXT NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fact_id, seq));
CREATE TABLE IF NOT EXISTS lineage_copies(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  copy_id TEXT NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fact_id, copy_id));
CREATE TABLE IF NOT EXISTS facts(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  key TEXT NOT NULL, value TEXT NOT NULL, category TEXT NOT NULL,
  source TEXT NOT NULL, excerpt TEXT NOT NULL, captured_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, row_version INTEGER NOT NULL,
  generation INTEGER NOT NULL, created_op TEXT NOT NULL,
  legacy_sensitive INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fact_id));
CREATE TABLE IF NOT EXISTS history(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  history_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  source_version INTEGER NOT NULL, key TEXT NOT NULL, value TEXT NOT NULL,
  category TEXT NOT NULL, source TEXT NOT NULL, excerpt TEXT NOT NULL,
  captured_at INTEGER NOT NULL, replacement_fact_id INTEGER,
  replacement_post_commit_version INTEGER, reason TEXT NOT NULL,
  operation_id TEXT NOT NULL, created_op TEXT NOT NULL,
  binding_hash TEXT NOT NULL, superseded_at_epoch INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, history_id));
CREATE TABLE IF NOT EXISTS marker_keys(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  fixture_key TEXT NOT NULL, created_at INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fixture_key));
CREATE TABLE IF NOT EXISTS markers(
  marker_key TEXT NOT NULL, uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  marker_class TEXT NOT NULL, key_ref TEXT NOT NULL,
  generation INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS optins(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, category TEXT NOT NULL,
  value INTEGER NOT NULL, actor TEXT NOT NULL, policy_revision INTEGER NOT NULL,
  operation_id TEXT NOT NULL, applied_at_revision INTEGER NOT NULL,
  updated_at INTEGER NOT NULL, row_version INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, category));
CREATE TABLE IF NOT EXISTS operation_records(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  operation_id TEXT NOT NULL, binding_hash TEXT NOT NULL, marker TEXT,
  method TEXT NOT NULL, result_code TEXT NOT NULL, outcome_json TEXT,
  outcome_kind TEXT, proof_json TEXT, applied_at_revision INTEGER,
  epoch INTEGER, store_revision INTEGER, fence INTEGER,
  scrubbed INTEGER NOT NULL, scrub_revision INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, operation_id));
CREATE TABLE IF NOT EXISTS attempt_records(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  operation_id TEXT NOT NULL, attempt_seq INTEGER NOT NULL,
  attempt_binding_hash TEXT NOT NULL, refusal TEXT, created_at INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, operation_id, attempt_seq));
CREATE TABLE IF NOT EXISTS abort_witnesses(
  operation_id TEXT NOT NULL, attempt_id TEXT NOT NULL, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, commitment TEXT NOT NULL,
  fence INTEGER NOT NULL, session_id TEXT NOT NULL, state TEXT NOT NULL,
  terminal_state TEXT, attested_at INTEGER, resolution_event_id TEXT,
  written_at INTEGER NOT NULL,
  PRIMARY KEY(operation_id, attempt_id));
CREATE TABLE IF NOT EXISTS attempt_registry(
  operation_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, attempt_id TEXT, session_id TEXT,
  commitment TEXT NOT NULL, state TEXT NOT NULL, fenced_by TEXT,
  fenced_at INTEGER, resolution_event_id TEXT, minted_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS insertion_events(
  event_id TEXT PRIMARY KEY, subject_evidence_ref TEXT NOT NULL,
  recovery_id TEXT NOT NULL, inserting_operation_id TEXT NOT NULL,
  inserting_uid INTEGER NOT NULL, inserting_lifetime_id INTEGER NOT NULL,
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  operation_id TEXT NOT NULL, commitment TEXT NOT NULL, source TEXT NOT NULL,
  inserted_count INTEGER NOT NULL, new_retained INTEGER NOT NULL,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS resolution_event_ledger(
  resolution_event_id TEXT NOT NULL, class_name TEXT NOT NULL,
  entry_id TEXT NOT NULL, inserted_at INTEGER NOT NULL,
  PRIMARY KEY(resolution_event_id, class_name, entry_id));
CREATE TABLE IF NOT EXISTS resolved_accounting(
  recovery_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, operation_id TEXT NOT NULL,
  commitment TEXT NOT NULL, resolution TEXT NOT NULL, source_json TEXT NOT NULL,
  resolution_event_id TEXT NOT NULL, resolved_count INTEGER NOT NULL,
  attested_witness_count INTEGER NOT NULL, new_retained INTEGER NOT NULL,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS blocked_records(
  recovery_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, operation_id TEXT NOT NULL,
  binding_hash TEXT NOT NULL, proof TEXT NOT NULL, resolver TEXT NOT NULL,
  deadline INTEGER NOT NULL, retention TEXT NOT NULL, state TEXT NOT NULL,
  evidence_count INTEGER NOT NULL, overflow_marker INTEGER NOT NULL,
  created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS evidence_entries(
  evidence_id TEXT PRIMARY KEY, recovery_id TEXT NOT NULL, kind TEXT NOT NULL,
  delivery_token TEXT NOT NULL, received_at INTEGER NOT NULL,
  ref TEXT NOT NULL, projection_json TEXT NOT NULL,
  insertion_event_id TEXT, resolution_event_id TEXT,
  origin_kind TEXT NOT NULL, inserting_uid INTEGER NOT NULL,
  inserting_lifetime_id INTEGER NOT NULL,
  inserting_operation_id TEXT NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS evidence_entries_delivery
  ON evidence_entries(recovery_id, delivery_token);
CREATE TABLE IF NOT EXISTS stored_requests(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  operation_id TEXT NOT NULL, request_json TEXT NOT NULL,
  binding_json TEXT NOT NULL, binding_hash TEXT NOT NULL,
  captured_at INTEGER NOT NULL, scrubbed INTEGER NOT NULL,
  scrub_revision INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, operation_id));
CREATE TABLE IF NOT EXISTS operation_receipts(
  op_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  receipt_json TEXT NOT NULL, ref TEXT NOT NULL, captured_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS copy_content(
  content_ref TEXT PRIMARY KEY, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, content TEXT NOT NULL,
  lineage TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS erasure_lineage_ops(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  seq INTEGER NOT NULL, op_id TEXT NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fact_id, seq));
CREATE TABLE IF NOT EXISTS erasure_lineage_copies(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, fact_id INTEGER NOT NULL,
  copy_id TEXT NOT NULL,
  PRIMARY KEY(uid, lifetime_id, fact_id, copy_id));
CREATE TABLE IF NOT EXISTS erasure_identities(
  uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL, class_name TEXT NOT NULL,
  entry_id TEXT NOT NULL, operation_id TEXT NOT NULL,
  binding_hash TEXT NOT NULL, generation INTEGER NOT NULL,
  created_op TEXT NOT NULL, last_op TEXT NOT NULL, scrubbed_at INTEGER NOT NULL,
  PRIMARY KEY(uid, lifetime_id, class_name, entry_id));
CREATE TABLE IF NOT EXISTS copy_inventory(
  copy_id TEXT PRIMARY KEY, uid INTEGER NOT NULL, lifetime_id INTEGER NOT NULL,
  kind TEXT NOT NULL, location TEXT NOT NULL, content_ref TEXT NOT NULL,
  lineage TEXT NOT NULL, version INTEGER NOT NULL, created_at INTEGER NOT NULL,
  scrubbed_at INTEGER);
CREATE TABLE IF NOT EXISTS cursor_registry(
  cursor_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, query_kind TEXT NOT NULL,
  filter_hash TEXT NOT NULL, query_revision INTEGER NOT NULL,
  position INTEGER NOT NULL, row_kind TEXT NOT NULL,
  issued_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS report_manifest(
  report_id TEXT PRIMARY KEY, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, operation_id TEXT NOT NULL,
  commit_revision INTEGER NOT NULL, new_epoch INTEGER NOT NULL,
  class_counts_json TEXT NOT NULL, page_count INTEGER NOT NULL,
  status TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS report_pages(
  report_id TEXT NOT NULL, page_index INTEGER NOT NULL, uid INTEGER NOT NULL,
  lifetime_id INTEGER NOT NULL, operation_id TEXT NOT NULL,
  class_name TEXT NOT NULL, range_start TEXT NOT NULL, range_end TEXT NOT NULL,
  commit_revision INTEGER NOT NULL, ids_json TEXT, status TEXT NOT NULL,
  PRIMARY KEY(report_id, page_index));
"""


# ---------------------------------------------------------------------------
# Closed enums (contract sec 6 - every value declared)
# ---------------------------------------------------------------------------

class MethodCode(Enum):
    PUT = "put"
    SUPERSEDE = "supersede"
    DELETE = "delete"
    WIPE = "wipe"
    SET_OPTIN = "set_optin"


class ModelIntent(Enum):
    WriteFact = "WriteFact"
    SupersedeFact = "SupersedeFact"
    DeleteFact = "DeleteFact"
    WipeOwner = "WipeOwner"
    SetOptIn = "SetOptIn"


_INTENT_FOR_METHOD = {
    MethodCode.PUT: ModelIntent.WriteFact,
    MethodCode.SUPERSEDE: ModelIntent.SupersedeFact,
    MethodCode.DELETE: ModelIntent.DeleteFact,
    MethodCode.WIPE: ModelIntent.WipeOwner,
    MethodCode.SET_OPTIN: ModelIntent.SetOptIn,
}


class ConflictField(Enum):
    VALUE = "value"
    CATEGORY = "category"
    SOURCE = "source"
    PROVENANCE = "provenance"


class Category(Enum):
    PREFERENCE = "preference"
    FACT = "fact"
    PROFILE = "profile"


class Source(Enum):
    WEB = "web"
    TELEGRAM = "telegram"


class Actor(Enum):
    OWNER = "owner"


class HistoryReason(Enum):
    SUPERSEDE = "supersede"


class RowKind(Enum):
    LIVE = "live"
    HISTORY = "history"


class CopyClass(Enum):
    FACTS = "facts"
    HISTORY = "history"
    PROVENANCE = "provenance"
    OPT_INS = "opt_ins"
    LOCAL_COPIES = "local_copies"
    OP_RECORDS = "op_records"
    CACHE = "cache"
    KEY_INVENTORY = "key_inventory"
    CURSOR_REGISTRY = "cursor_registry"
    REPORT_PAGES = "report_pages"
    STORED_REQUESTS = "stored_requests"
    COPY_CONTENT = "copy_content"
    RESOLVED_ACCOUNTING = "resolved_accounting"
    ABORT_WITNESSES = "abort_witnesses"
    ATTEMPT_REGISTRY = "attempt_registry"
    RESOLUTION_EVENT_LEDGER = "resolution_event_ledger"
    INSERTION_EVENTS = "insertion_events"


class WipeScope(Enum):
    OWNER_MEMORY_FULL = "owner_memory_full"


class UnknownCoverage(Enum):
    PROVIDER_SIDE = "provider_side"
    UNAUDITED_EXTERNAL_COPY = "unaudited_external_copy"
    DERIVED_SUMMARY_STATE = "derived_summary_state"


class ExternalRevocation(Enum):
    NOT_APPLICABLE = "not_applicable"
    ATTEMPTED_UNKNOWN = "attempted_unknown"


class FutureDerivationState(Enum):
    NO_DERIVED_STATE = "no_derived_state"
    EXCLUSIONS_RETAINED = "exclusions_retained"


class RetainedClassName(Enum):
    OWNER_COUNTERS = "owner_counters"
    STORE_REVISION_COUNTER = "store_revision_counter"
    LIFETIME = "lifetime"
    MARKER_KEYS = "marker_keys"
    MARKER_ENTRIES = "marker_entries"
    TOMBSTONES = "tombstones"
    OP_RECORD_NONCONTENT = "op_record_noncontent"
    ACCOUNT_AUTH_FIXTURES = "account_auth_fixtures"
    SAFE_EFFECT_METADATA = "safe_effect_metadata"


class ErasedReadback(Enum):
    ERASED = "erased"


class ReadbackCause(Enum):
    PROTOCOL = "protocol"
    OPERATIONAL = "operational"


class ResolverCode(Enum):
    MANUAL = "manual"
    SCHEDULED = "scheduled"
    READBACK = "readback"


class RetentionCode(Enum):
    STANDARD = "standard"
    EXTENDED = "extended"
    WIPED = "wiped"


class EvidenceKind(Enum):
    RECEIPT = "receipt"
    CONFLICTING_RECEIPT = "conflicting_receipt"
    LATE_ACCOUNTING = "late_accounting"
    VERIFIED_ROLLBACK = "verified_rollback"


class QueryKind(Enum):
    FACTS = "facts"
    REPORT_PAGE = "report_page"


class ReadMethod(Enum):
    GET = "get"
    GET_OPTIN = "get_optin"
    LIST_FACTS = "list_facts"
    COUNT_FACTS = "count_facts"
    READ_REPORT_PAGE = "read_report_page"
    READBACK = "readback"
    RESOLVE = "resolve"
    PREPARE_CLOSURE = "prepare_closure"


# ---------------------------------------------------------------------------
# PR-5 FactStore-level errors (contract sec 8)
# ---------------------------------------------------------------------------

class FactConflict(Exception):
    def __init__(self, code: str) -> None:
        if code not in ("CAS", "ERASED"):
            raise cap.ModelInputError()
        super().__init__(code)
        self.code = code


class UnknownCursorError(Exception):
    pass


class UnknownOperationError(Exception):
    pass


class DeliveryDriftError(Exception):
    pass


class UnknownRecoveryError(Exception):
    """insert_evidence: the recovery_id has NO CURRENT blocked record and
    NO known (resolved) recovery - the typed unknown-recovery refusal."""


class EvidenceScopeError(Exception):
    """insert_evidence: the subject hash-owner-accounting scope or the
    recovery target join does NOT bind to the owner accounting - the
    typed refusal (sec 5; a forged uid/lifetime/recovery never lands)."""


class HarnessRefusedError(Exception):
    """The issuer-harness FIVE-STAGE equality REFUSAL: a mismatch at ANY
    stage (compose/persist/send/restart/receiver checks) - no mint, no
    send, no ack, no count, no state change."""

    def __init__(self, stage: str, reason: str) -> None:
        super().__init__(stage + ":" + reason)
        self.stage = stage
        self.reason = reason


class ReadbackUnavailableError(Exception):
    def __init__(self, known_outcome, known_revision: int,
                 cause: ReadbackCause) -> None:
        _req_enum(cause, ReadbackCause)
        _req_int(known_revision, 1)
        super().__init__(cause.value)
        self.known_outcome = known_outcome
        self.known_revision = known_revision
        self.cause = cause


# ---------------------------------------------------------------------------
# Core types (contract sec 7)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Lifetime:
    uid: int
    lifetime_id: int

    def __post_init__(self) -> None:
        _req_int(self.uid, 0)
        _req_int(self.lifetime_id, 1)


@dataclass(frozen=True)
class Provenance:
    excerpt: str
    captured_at: int

    def __post_init__(self) -> None:
        _req_str(self.excerpt, EXCERPT_MAX, required=False)
        _req_int(self.captured_at, 0)


FactId = int
Version = int


@dataclass(frozen=True)
class NewFact:
    key: str
    value: str
    category: Category
    source: Source
    provenance: Provenance

    def __post_init__(self) -> None:
        _req_str(self.key, KEY_MAX * 4)
        _normalize_key(self.key)
        _req_str(self.value, VALUE_MAX)
        _req_enum(self.category, Category)
        _req_enum(self.source, Source)
        if type(self.provenance) is not Provenance:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class FactReadback:
    fact_id: int
    key: str
    value: str
    category: Category
    source: Source
    provenance: Provenance
    updated_at: int
    row_version: int

    def __post_init__(self) -> None:
        _req_int(self.fact_id, 1)
        _req_str(self.key, KEY_MAX)
        _req_str(self.value, VALUE_MAX, required=False)
        _req_enum(self.category, Category)
        _req_enum(self.source, Source)
        if type(self.provenance) is not Provenance:
            raise cap.ModelInputError()
        _req_int(self.updated_at, 0)
        _req_int(self.row_version, 1)


@dataclass(frozen=True)
class HistoryRow:
    history_id: int
    fact_id: int
    source_version: int
    key: str
    value: str
    category: Category
    source: Source
    excerpt: str
    captured_at: int
    replacement_fact_id: Optional[int]
    replacement_post_commit_version: Optional[int]
    reason: HistoryReason
    operation_id: str
    created_op: str
    binding_hash: str
    superseded_at_epoch: int

    def __post_init__(self) -> None:
        _req_int(self.history_id, 1)
        _req_int(self.fact_id, 1)
        _req_int(self.source_version, 1)
        _req_str(self.key, KEY_MAX)
        _req_str(self.value, VALUE_MAX, required=False)
        _req_enum(self.category, Category)
        _req_enum(self.source, Source)
        _req_str(self.excerpt, EXCERPT_MAX, required=False)
        _req_int(self.captured_at, 0)
        if self.replacement_fact_id is not None:
            _req_int(self.replacement_fact_id, 1)
        if self.replacement_post_commit_version is not None:
            _req_int(self.replacement_post_commit_version, 1)
        _req_enum(self.reason, HistoryReason)
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_hex(self.created_op, OP_ID_LEN)
        _req_hex(self.binding_hash, HASH_LEN)
        _req_int(self.superseded_at_epoch, 0)


@dataclass(frozen=True)
class OptinRecord:
    owner: Lifetime
    category: Category
    value: bool
    actor: Actor
    policy_revision: int
    operation_id: str
    applied_at_revision: int
    updated_at: int

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_enum(self.category, Category)
        _req_bool(self.value)
        _req_enum(self.actor, Actor)
        _req_int(self.policy_revision, 0)
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_int(self.applied_at_revision, 1)
        _req_int(self.updated_at, 0)


@dataclass(frozen=True)
class OptInSetting:
    category: Category

    def __post_init__(self) -> None:
        _req_enum(self.category, Category)


@dataclass(frozen=True)
class ClosureCAS:
    live: Optional[int]
    lineage_digest: str

    def __post_init__(self) -> None:
        if self.live is not None:
            _req_int(self.live, 1)
        _req_hex(self.lineage_digest, HASH_LEN)


# -- Request payloads (sec 7) -----------------------------------------------

@dataclass(frozen=True)
class PutPayload:
    fact: NewFact

    def __post_init__(self) -> None:
        if type(self.fact) is not NewFact:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class SupersedePayload:
    replacement: NewFact
    old_fids: Tuple[int, ...]
    expected_versions: dict

    def __post_init__(self) -> None:
        if type(self.replacement) is not NewFact:
            raise cap.ModelInputError()
        if type(self.old_fids) not in (tuple, list):
            raise cap.ModelInputError()
        fids = tuple(self.old_fids)
        if not OLD_FIDS_MIN <= len(fids) <= OLD_FIDS_MAX:
            raise cap.ModelInputError()
        for fid in fids:
            _req_int(fid, 1)
        if len(set(fids)) != len(fids) or fids != tuple(sorted(fids)):
            raise cap.ModelInputError()
        object.__setattr__(self, "old_fids", fids)
        if type(self.expected_versions) is not dict:
            raise cap.ModelInputError()
        if set(self.expected_versions.keys()) != set(fids):
            raise cap.ModelInputError()
        for fid, ver in self.expected_versions.items():
            _req_int(fid, 1)
            _req_int(ver, 1)


@dataclass(frozen=True)
class DeletePayload:
    fid: int
    closure_cas: ClosureCAS

    def __post_init__(self) -> None:
        _req_int(self.fid, 1)
        if type(self.closure_cas) is not ClosureCAS:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class WipePayload:
    expected_target_versions: dict

    def __post_init__(self) -> None:
        if type(self.expected_target_versions) is not dict:
            raise cap.ModelInputError()
        if len(self.expected_target_versions) > 500:
            raise cap.ModelInputError()
        for fid, ver in self.expected_target_versions.items():
            _req_int(fid, 1)
            _req_int(ver, 1)


@dataclass(frozen=True)
class OptInPayload:
    setting: OptInSetting
    value: bool

    def __post_init__(self) -> None:
        if type(self.setting) is not OptInSetting:
            raise cap.ModelInputError()
        _req_bool(self.value)


_PAYLOAD_TYPES = {
    MethodCode.PUT: PutPayload,
    MethodCode.SUPERSEDE: SupersedePayload,
    MethodCode.DELETE: DeletePayload,
    MethodCode.WIPE: WipePayload,
    MethodCode.SET_OPTIN: OptInPayload,
}


@dataclass(frozen=True)
class FactRequest:
    """The ONLY mutation input (sec 7 REQUEST SUM) plus the COMPARED
    envelope fields {owner_uid, lifetime_id, operation_id, information_ref}."""
    method: MethodCode
    intent: ModelIntent
    payload: object
    owner_uid: int
    lifetime_id: int
    operation_id: str
    information_ref: str

    def __post_init__(self) -> None:
        _req_enum(self.method, MethodCode)
        _req_enum(self.intent, ModelIntent)
        if _INTENT_FOR_METHOD[self.method] is not self.intent:
            raise cap.ModelInputError()
        if type(self.payload) is not _PAYLOAD_TYPES[self.method]:
            raise cap.ModelInputError()
        _req_int(self.owner_uid, 0)
        _req_int(self.lifetime_id, 1)
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_str(self.information_ref, BOUNDED_STR_MAX)


# ---------------------------------------------------------------------------
# Canonical serialization (sec 4b/7) + extended commitment (sec 4)
# ---------------------------------------------------------------------------

def _provenance_dict(prov: Provenance) -> dict:
    return {"captured_at": prov.captured_at, "excerpt": prov.excerpt}


def _provenance_from_dict(obj) -> Provenance:
    if type(obj) is not dict or set(obj.keys()) != {"captured_at", "excerpt"}:
        raise cap.ModelInputError()
    return Provenance(excerpt=obj["excerpt"], captured_at=obj["captured_at"])


def _new_fact_dict(fact: NewFact) -> dict:
    return {
        "category": fact.category.value,
        "key": _normalize_key(fact.key),
        "provenance": _provenance_dict(fact.provenance),
        "source": fact.source.value,
        "value": fact.value,
    }


def _payload_dict(request: FactRequest) -> dict:
    payload = request.payload
    if request.method is MethodCode.PUT:
        return {"fact": _new_fact_dict(payload.fact)}
    if request.method is MethodCode.SUPERSEDE:
        return {
            "expected_versions": {str(fid): ver for fid, ver in
                                  sorted(payload.expected_versions.items())},
            "old_fids": list(payload.old_fids),
            "replacement": _new_fact_dict(payload.replacement),
        }
    if request.method is MethodCode.DELETE:
        return {
            "closure_cas": {
                "lineage_digest": payload.closure_cas.lineage_digest,
                "live": payload.closure_cas.live,
            },
            "fid": payload.fid,
        }
    if request.method is MethodCode.WIPE:
        return {"expected_target_versions":
                {str(fid): ver for fid, ver in
                 sorted(payload.expected_target_versions.items())}}
    return {"setting": {"category": payload.setting.category.value},
            "value": payload.value}


def request_dict(request: FactRequest) -> dict:
    """The canonical stored FactRequest shape (sec 7)."""
    return {
        "information_ref": request.information_ref,
        "intent": request.intent.value,
        "method": request.method.value,
        "operation_id": request.operation_id,
        "owner": {"lifetime_id": request.lifetime_id, "uid": request.owner_uid},
        "payload": _payload_dict(request),
    }


def request_canonical_json(request: FactRequest) -> str:
    return _canon(request_dict(request)).decode("utf-8")


def extended_binding_hash(binding: "env.ModelOnlyRequestBinding",
                          request: FactRequest) -> str:
    """The sec-4 EXTENDED commitment: SHA-256 over canonical JSON of the
    COMPLETE PR-4 projection PLUS the canonical PR-5 method + targets +
    versions + payload. Transport fields excluded (the PR-4 projection
    carries none)."""
    return _sha256_hex(_canon({
        "binding": binding.projection_dict(),
        "pr5": {"method": request.method.value,
                "payload": _payload_dict(request)},
    }))


def _content_fingerprint(request: FactRequest) -> str:
    return _sha256_hex(_canon(_payload_dict(request)))


def _readback_dict(row: FactReadback) -> dict:
    return {
        "category": row.category.value,
        "fact_id": row.fact_id,
        "key": row.key,
        "provenance": _provenance_dict(row.provenance),
        "row_version": row.row_version,
        "source": row.source.value,
        "updated_at": row.updated_at,
        "value": row.value,
    }


def _readback_from_dict(obj) -> FactReadback:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        return FactReadback(
            fact_id=obj["fact_id"], key=obj["key"], value=obj["value"],
            category=Category(obj["category"]), source=Source(obj["source"]),
            provenance=_provenance_from_dict(obj["provenance"]),
            updated_at=obj["updated_at"], row_version=obj["row_version"])
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def _optin_record_dict(record: OptinRecord) -> dict:
    return {
        "actor": record.actor.value,
        "applied_at_revision": record.applied_at_revision,
        "category": record.category.value,
        "operation_id": record.operation_id,
        "owner": {"lifetime_id": record.owner.lifetime_id,
                  "uid": record.owner.uid},
        "policy_revision": record.policy_revision,
        "updated_at": record.updated_at,
        "value": record.value,
    }


def _optin_record_from_dict(obj) -> OptinRecord:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        owner = obj["owner"]
        return OptinRecord(
            owner=Lifetime(uid=owner["uid"],
                           lifetime_id=owner["lifetime_id"]),
            category=Category(obj["category"]), value=obj["value"],
            actor=Actor(obj["actor"]), policy_revision=obj["policy_revision"],
            operation_id=obj["operation_id"],
            applied_at_revision=obj["applied_at_revision"],
            updated_at=obj["updated_at"])
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def _class_counts_dict(counts: dict) -> dict:
    """ClassCounts = dict[CopyClass, {physically_removed, content_scrubbed}]."""
    if type(counts) is not dict:
        raise cap.ModelInputError()
    out = {}
    for cls, pair in counts.items():
        _req_enum(cls, CopyClass)
        if type(pair) is not dict:
            raise cap.ModelInputError()
        _req_int(pair.get("physically_removed"), 0)
        _req_int(pair.get("content_scrubbed"), 0)
        out[cls] = {"content_scrubbed": pair["content_scrubbed"],
                    "physically_removed": pair["physically_removed"]}
    return out


def _class_counts_json(counts: dict) -> dict:
    return {cls.value: _class_counts_dict({cls: pair})[cls]
            for cls, pair in
            sorted(counts.items(), key=lambda kv: kv[0].value)}


def _class_counts_from_json(obj) -> dict:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    out = {}
    for name, pair in obj.items():
        try:
            cls = CopyClass(name)
        except ValueError:
            raise cap.ModelInputError()
        out[cls] = _class_counts_dict({cls: pair})[cls]
    return out


def _empty_class_counts() -> dict:
    return {}


@dataclass(frozen=True)
class DeleteProof:
    closure_counts: dict          # ClassCounts
    new_retained: int
    scrubbed_fids: Tuple[int, ...]
    report_ref: str

    def __post_init__(self) -> None:
        _class_counts_dict(self.closure_counts)
        _req_int(self.new_retained, 0)
        fids = tuple(self.scrubbed_fids)
        for fid in fids:
            _req_int(fid, 1)
        if fids != tuple(sorted(set(fids))):
            raise cap.ModelInputError()
        object.__setattr__(self, "scrubbed_fids", fids)
        _req_str(self.report_ref, BOUNDED_STR_MAX)

    def to_dict(self) -> dict:
        return {"closure_counts": _class_counts_json(self.closure_counts),
                "new_retained": self.new_retained,
                "report_ref": self.report_ref,
                "scrubbed_fids": list(self.scrubbed_fids)}


@dataclass(frozen=True)
class SafeDeleteProof:
    closure_counts: dict
    new_retained: int
    scrubbed_fids: Tuple[int, ...]
    report_ref: str

    def __post_init__(self) -> None:
        _class_counts_dict(self.closure_counts)
        _req_int(self.new_retained, 0)
        fids = tuple(self.scrubbed_fids)
        for fid in fids:
            _req_int(fid, 1)
        if fids != tuple(sorted(set(fids))):
            raise cap.ModelInputError()
        object.__setattr__(self, "scrubbed_fids", fids)
        _req_str(self.report_ref, BOUNDED_STR_MAX)

    def to_dict(self) -> dict:
        return {"closure_counts": _class_counts_json(self.closure_counts),
                "new_retained": self.new_retained,
                "report_ref": self.report_ref,
                "scrubbed_fids": list(self.scrubbed_fids)}


@dataclass(frozen=True)
class InventoryRef:
    class_name: CopyClass
    entry_id: str
    owner: Lifetime

    def __post_init__(self) -> None:
        _req_enum(self.class_name, CopyClass)
        _req_str(self.entry_id, BOUNDED_STR_MAX)
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()

    def to_dict(self) -> dict:
        return {"class_name": self.class_name.value,
                "entry_id": self.entry_id,
                "owner": {"lifetime_id": self.owner.lifetime_id,
                          "uid": self.owner.uid}}


@dataclass(frozen=True)
class ReportRange:
    report_id: str
    owner: Lifetime
    operation_id: str
    class_name: CopyClass
    commit_revision: int
    page_index: int
    page_size: int
    total_count: int
    per_class_totals: dict
    new_retained: int
    ids: Tuple[InventoryRef, ...]

    def __post_init__(self) -> None:
        _req_str(self.report_id, BOUNDED_STR_MAX)
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_enum(self.class_name, CopyClass)
        _req_int(self.commit_revision, 1)
        _req_int(self.page_index, 0)
        _req_int(self.page_size, 1, PAGE_MAX)
        _req_int(self.total_count, 0)
        _class_counts_dict(self.per_class_totals)
        _req_int(self.new_retained, 0)
        ids = tuple(self.ids)
        if len(ids) > PAGE_MAX:
            raise cap.ModelInputError()
        keys = []
        for ref in ids:
            if type(ref) is not InventoryRef:
                raise cap.ModelInputError()
            keys.append((ref.class_name.value, ref.entry_id))
        if keys != sorted(set(keys)):
            raise cap.ModelInputError()
        object.__setattr__(self, "ids", ids)


@dataclass(frozen=True)
class RetainedClass:
    name: RetainedClassName
    reason: str

    def __post_init__(self) -> None:
        _req_enum(self.name, RetainedClassName)
        _req_str(self.reason, 128)

    def to_dict(self) -> dict:
        return {"name": self.name.value, "reason": self.reason}


@dataclass(frozen=True)
class WipeReport:
    owner: Lifetime
    operation_id: str
    new_epoch: int
    report_id: str
    page_count: int
    counts: dict
    new_retained: int
    commit_revision: int
    scope: WipeScope
    first_page: ReportRange
    retained: Tuple[RetainedClass, ...]
    unknown_coverage: Tuple[UnknownCoverage, ...]
    external_revocations: Tuple[ExternalRevocation, ...]
    future_derivation_state: FutureDerivationState

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_int(self.new_epoch, 1)
        _req_str(self.report_id, BOUNDED_STR_MAX)
        _req_int(self.page_count, 1)
        _class_counts_dict(self.counts)
        _req_int(self.new_retained, 0)
        _req_int(self.commit_revision, 1)
        _req_enum(self.scope, WipeScope)
        if type(self.first_page) is not ReportRange:
            raise cap.ModelInputError()
        retained = tuple(self.retained)
        if len(retained) > RETAINED_MAX:
            raise cap.ModelInputError()
        for entry in retained:
            if type(entry) is not RetainedClass:
                raise cap.ModelInputError()
        object.__setattr__(self, "retained", retained)
        coverage = tuple(self.unknown_coverage)
        if len(coverage) > UNKNOWN_COVERAGE_MAX:
            raise cap.ModelInputError()
        for entry in coverage:
            _req_enum(entry, UnknownCoverage)
        object.__setattr__(self, "unknown_coverage", coverage)
        revocations = tuple(self.external_revocations)
        for entry in revocations:
            _req_enum(entry, ExternalRevocation)
        object.__setattr__(self, "external_revocations", revocations)
        _req_enum(self.future_derivation_state, FutureDerivationState)


@dataclass(frozen=True)
class SafeWipeReport:
    owner: Lifetime
    operation_id: str
    new_epoch: int
    counts: dict
    new_retained: int
    commit_revision: int
    scope: WipeScope
    report_id: str
    page_count: int
    retained: Tuple[RetainedClassName, ...]
    unknown_coverage: Tuple[UnknownCoverage, ...]
    external_revocations: Tuple[ExternalRevocation, ...]
    future_derivation_state: FutureDerivationState

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_int(self.new_epoch, 1)
        _class_counts_dict(self.counts)
        _req_int(self.new_retained, 0)
        _req_int(self.commit_revision, 1)
        _req_enum(self.scope, WipeScope)
        _req_str(self.report_id, BOUNDED_STR_MAX)
        _req_int(self.page_count, 1)
        retained = tuple(self.retained)
        if len(retained) > RETAINED_MAX:
            raise cap.ModelInputError()
        for entry in retained:
            _req_enum(entry, RetainedClassName)
        object.__setattr__(self, "retained", retained)
        coverage = tuple(self.unknown_coverage)
        if len(coverage) > UNKNOWN_COVERAGE_MAX:
            raise cap.ModelInputError()
        for entry in coverage:
            _req_enum(entry, UnknownCoverage)
        object.__setattr__(self, "unknown_coverage", coverage)
        revocations = tuple(self.external_revocations)
        for entry in revocations:
            _req_enum(entry, ExternalRevocation)
        object.__setattr__(self, "external_revocations", revocations)
        _req_enum(self.future_derivation_state, FutureDerivationState)


# ---------------------------------------------------------------------------
# OUTCOME UNIONS (sec 7) - flat dataclasses with EXACT per-variant invariants
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class OperationOutcome:
    """OperationOutcome (committed, FULL inline result). One object; the
    variant invariants below make the members DISJOINT."""
    method: MethodCode
    result_code: str
    applied_at_revision: Optional[int] = None
    readback: Optional[FactReadback] = None
    conflict_fields: Tuple[ConflictField, ...] = ()
    superseded: Tuple[int, ...] = ()
    collided_fid: Optional[int] = None
    new_epoch: Optional[int] = None
    proof: Optional[DeleteProof] = None
    report: Optional[WipeReport] = None
    record: Optional[OptinRecord] = None

    def __post_init__(self) -> None:
        _req_enum(self.method, MethodCode)
        allowed = {
            MethodCode.PUT: ("unchanged", "applied", "conflict"),
            MethodCode.SUPERSEDE: ("applied", "conflict"),
            MethodCode.DELETE: ("applied", "absent"),
            MethodCode.WIPE: ("applied",),
            MethodCode.SET_OPTIN: ("applied", "unchanged"),
        }[self.method]
        if self.result_code not in allowed:
            raise cap.ModelInputError()
        applied = self.result_code == "applied"
        if applied:
            _req_int(self.applied_at_revision, 1)
        elif self.applied_at_revision is not None:
            raise cap.ModelInputError()
        fields = tuple(self.conflict_fields)
        for entry in fields:
            _req_enum(entry, ConflictField)
        if fields != tuple(sorted(set(fields), key=lambda f: f.value)):
            raise cap.ModelInputError()
        object.__setattr__(self, "conflict_fields", fields)
        superseded = tuple(self.superseded)
        for fid in superseded:
            _req_int(fid, 1)
        object.__setattr__(self, "superseded", superseded)
        if self.collided_fid is not None:
            _req_int(self.collided_fid, 1)
        if self.new_epoch is not None:
            _req_int(self.new_epoch, 1)
        # per-variant field presence
        if self.method is MethodCode.PUT:
            if self.proof is not None or self.report is not None \
                    or self.record is not None or self.superseded \
                    or self.collided_fid is not None \
                    or self.new_epoch is not None:
                raise cap.ModelInputError()
            if applied and type(self.readback) is not FactReadback:
                raise cap.ModelInputError()
            if not applied:
                if self.readback is not None:
                    raise cap.ModelInputError()
                if self.result_code == "conflict" and not fields:
                    raise cap.ModelInputError()
                if self.result_code == "unchanged" and fields:
                    raise cap.ModelInputError()
        elif self.method is MethodCode.SUPERSEDE:
            if self.proof is not None or self.report is not None \
                    or self.record is not None or self.new_epoch is not None:
                raise cap.ModelInputError()
            if applied:
                if type(self.readback) is not FactReadback:
                    raise cap.ModelInputError()
                if fields or self.collided_fid is not None:
                    raise cap.ModelInputError()
            else:
                if self.readback is not None or self.superseded \
                        or self.collided_fid is None:
                    raise cap.ModelInputError()
        elif self.method is MethodCode.DELETE:
            if self.readback is not None or fields or self.superseded \
                    or self.collided_fid is not None \
                    or self.report is not None or self.record is not None:
                raise cap.ModelInputError()
            if applied:
                _req_int(self.new_epoch, 1)
                if type(self.proof) is not DeleteProof:
                    raise cap.ModelInputError()
            else:
                if self.new_epoch is not None or self.proof is not None:
                    raise cap.ModelInputError()
        elif self.method is MethodCode.WIPE:
            if self.readback is not None or fields or self.superseded \
                    or self.collided_fid is not None \
                    or self.proof is not None or self.record is not None:
                raise cap.ModelInputError()
            _req_int(self.new_epoch, 1)
            if type(self.report) is not WipeReport:
                raise cap.ModelInputError()
        else:  # set_optin
            if self.readback is not None or fields or self.superseded \
                    or self.collided_fid is not None \
                    or self.proof is not None or self.report is not None \
                    or self.new_epoch is not None:
                raise cap.ModelInputError()
            if applied and type(self.record) is not OptinRecord:
                raise cap.ModelInputError()
            if not applied and self.record is not None \
                    and type(self.record) is not OptinRecord:
                raise cap.ModelInputError()

    def to_dict(self) -> dict:
        out = {"method": self.method.value,
               "result_code": self.result_code,
               "applied_at_revision": self.applied_at_revision}
        if self.method is MethodCode.PUT:
            out["readback"] = (_readback_dict(self.readback)
                               if self.readback is not None else None)
            if not self.result_code == "applied":
                out["conflict_fields"] = [f.value for f in
                                          self.conflict_fields]
        elif self.method is MethodCode.SUPERSEDE:
            out["readback"] = (_readback_dict(self.readback)
                               if self.readback is not None else None)
            out["superseded"] = list(self.superseded)
            if self.result_code == "conflict":
                out["collided_fid"] = self.collided_fid
        elif self.method is MethodCode.DELETE:
            out["new_epoch"] = self.new_epoch
            out["proof"] = (self.proof.to_dict()
                            if self.proof is not None else None)
        elif self.method is MethodCode.WIPE:
            out["new_epoch"] = self.new_epoch
            out["report"] = _wipe_report_dict(self.report)
        else:
            out["record"] = (_optin_record_dict(self.record)
                             if self.record is not None else None)
        return out


@dataclass(frozen=True)
class ScrubbedOutcome:
    """ScrubbedOutcome (post-erasure): EVERY content position holds the
    'erased' literal; safe immutable fields only; DISJOINT members."""
    method: MethodCode
    result_code: str
    applied_at_revision: Optional[int] = None
    conflict_fields: Tuple[ConflictField, ...] = ()
    superseded: Tuple[int, ...] = ()
    collided_fid: Optional[int] = None
    new_epoch: Optional[int] = None
    safe_proof: Optional[SafeDeleteProof] = None
    safe_report: Optional[SafeWipeReport] = None

    def __post_init__(self) -> None:
        _req_enum(self.method, MethodCode)
        allowed = {
            MethodCode.PUT: ("unchanged", "applied", "conflict"),
            MethodCode.SUPERSEDE: ("applied", "conflict"),
            MethodCode.DELETE: ("applied", "absent"),
            MethodCode.WIPE: ("applied",),
            MethodCode.SET_OPTIN: ("applied", "unchanged"),
        }[self.method]
        if self.result_code not in allowed:
            raise cap.ModelInputError()
        applied = self.result_code == "applied"
        if applied:
            _req_int(self.applied_at_revision, 1)
        elif self.applied_at_revision is not None:
            raise cap.ModelInputError()
        fields = tuple(self.conflict_fields)
        for entry in fields:
            _req_enum(entry, ConflictField)
        if fields != tuple(sorted(set(fields), key=lambda f: f.value)):
            raise cap.ModelInputError()
        object.__setattr__(self, "conflict_fields", fields)
        superseded = tuple(self.superseded)
        for fid in superseded:
            _req_int(fid, 1)
        object.__setattr__(self, "superseded", superseded)
        if self.collided_fid is not None:
            _req_int(self.collided_fid, 1)
        if self.new_epoch is not None:
            _req_int(self.new_epoch, 1)
        if self.method is MethodCode.DELETE:
            if applied:
                _req_int(self.new_epoch, 1)
                if type(self.safe_proof) is not SafeDeleteProof:
                    raise cap.ModelInputError()
            else:
                if (self.new_epoch is not None
                        or self.safe_proof is not None):
                    raise cap.ModelInputError()
        elif self.method is MethodCode.WIPE:
            _req_int(self.new_epoch, 1)
            if type(self.safe_report) is not SafeWipeReport:
                raise cap.ModelInputError()
        else:
            if (self.safe_proof is not None or self.safe_report is not None
                    or self.new_epoch is not None):
                raise cap.ModelInputError()

    def to_dict(self) -> dict:
        out = {"method": self.method.value,
               "result_code": self.result_code,
               "applied_at_revision": self.applied_at_revision}
        if self.method is MethodCode.PUT:
            out["readback"] = "erased"
            if self.result_code != "applied":
                out["conflict_fields"] = [f.value for f in
                                          self.conflict_fields]
        elif self.method is MethodCode.SUPERSEDE:
            out["readback"] = "erased"
            out["superseded"] = list(self.superseded)
            if self.result_code == "conflict":
                out["collided_fid"] = self.collided_fid
        elif self.method is MethodCode.DELETE:
            out["new_epoch"] = self.new_epoch
            out["safe_proof"] = (self.safe_proof.to_dict()
                                 if self.safe_proof is not None else None)
        elif self.method is MethodCode.WIPE:
            out["new_epoch"] = self.new_epoch
            out["report"] = _safe_wipe_report_dict(self.safe_report)
        return out


def _wipe_report_dict(report: WipeReport) -> dict:
    return {
        "commit_revision": report.commit_revision,
        "counts": _class_counts_json(report.counts),
        "external_revocations": [r.value for r in
                                 report.external_revocations],
        "first_page": _report_range_dict(report.first_page),
        "future_derivation_state": report.future_derivation_state.value,
        "new_epoch": report.new_epoch,
        "new_retained": report.new_retained,
        "operation_id": report.operation_id,
        "owner": {"lifetime_id": report.owner.lifetime_id,
                  "uid": report.owner.uid},
        "page_count": report.page_count,
        "report_id": report.report_id,
        "retained": [r.to_dict() for r in report.retained],
        "scope": report.scope.value,
        "unknown_coverage": [c.value for c in report.unknown_coverage],
    }


def _safe_wipe_report_dict(report: SafeWipeReport) -> dict:
    return {
        "commit_revision": report.commit_revision,
        "counts": _class_counts_json(report.counts),
        "external_revocations": [r.value for r in
                                 report.external_revocations],
        "future_derivation_state": report.future_derivation_state.value,
        "new_epoch": report.new_epoch,
        "new_retained": report.new_retained,
        "operation_id": report.operation_id,
        "owner": {"lifetime_id": report.owner.lifetime_id,
                  "uid": report.owner.uid},
        "page_count": report.page_count,
        "report_id": report.report_id,
        "retained": [r.value for r in report.retained],
        "scope": report.scope.value,
        "unknown_coverage": [c.value for c in report.unknown_coverage],
    }


def _report_range_dict(rng: ReportRange) -> dict:
    return {
        "class_name": rng.class_name.value,
        "commit_revision": rng.commit_revision,
        "ids": [ref.to_dict() for ref in rng.ids],
        "new_retained": rng.new_retained,
        "operation_id": rng.operation_id,
        "owner": {"lifetime_id": rng.owner.lifetime_id,
                  "uid": rng.owner.uid},
        "page_index": rng.page_index,
        "page_size": rng.page_size,
        "per_class_totals": _class_counts_json(rng.per_class_totals),
        "report_id": rng.report_id,
        "total_count": rng.total_count,
    }


def _wipe_report_from_dict(obj) -> WipeReport:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        return WipeReport(
            owner=Lifetime(uid=obj["owner"]["uid"],
                           lifetime_id=obj["owner"]["lifetime_id"]),
            operation_id=obj["operation_id"], new_epoch=obj["new_epoch"],
            report_id=obj["report_id"], page_count=obj["page_count"],
            counts=_class_counts_from_json(obj["counts"]),
            new_retained=obj["new_retained"],
            commit_revision=obj["commit_revision"],
            scope=WipeScope(obj["scope"]),
            first_page=_report_range_from_dict(obj["first_page"]),
            retained=tuple(RetainedClass(name=RetainedClassName(r["name"]),
                                         reason=r["reason"])
                           for r in obj["retained"]),
            unknown_coverage=tuple(UnknownCoverage(c)
                                   for c in obj["unknown_coverage"]),
            external_revocations=tuple(ExternalRevocation(r)
                                       for r in obj["external_revocations"]),
            future_derivation_state=FutureDerivationState(
                obj["future_derivation_state"]))
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def _safe_wipe_report_from_dict(obj) -> SafeWipeReport:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        return SafeWipeReport(
            owner=Lifetime(uid=obj["owner"]["uid"],
                           lifetime_id=obj["owner"]["lifetime_id"]),
            operation_id=obj["operation_id"], new_epoch=obj["new_epoch"],
            counts=_class_counts_from_json(obj["counts"]),
            new_retained=obj["new_retained"],
            commit_revision=obj["commit_revision"],
            scope=WipeScope(obj["scope"]),
            report_id=obj["report_id"], page_count=obj["page_count"],
            retained=tuple(RetainedClassName(r) for r in obj["retained"]),
            unknown_coverage=tuple(UnknownCoverage(c)
                                   for c in obj["unknown_coverage"]),
            external_revocations=tuple(ExternalRevocation(r)
                                       for r in obj["external_revocations"]),
            future_derivation_state=FutureDerivationState(
                obj["future_derivation_state"]))
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def _report_range_from_dict(obj) -> ReportRange:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        return ReportRange(
            report_id=obj["report_id"],
            owner=Lifetime(uid=obj["owner"]["uid"],
                           lifetime_id=obj["owner"]["lifetime_id"]),
            operation_id=obj["operation_id"],
            class_name=CopyClass(obj["class_name"]),
            commit_revision=obj["commit_revision"],
            page_index=obj["page_index"], page_size=obj["page_size"],
            total_count=obj["total_count"],
            per_class_totals=_class_counts_from_json(
                obj["per_class_totals"]),
            new_retained=obj["new_retained"],
            ids=tuple(InventoryRef(
                class_name=CopyClass(ref["class_name"]),
                entry_id=ref["entry_id"],
                owner=Lifetime(uid=ref["owner"]["uid"],
                               lifetime_id=ref["owner"]["lifetime_id"]))
                for ref in obj["ids"]))
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def _delete_proof_from_dict(obj) -> DeleteProof:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        return DeleteProof(
            closure_counts=_class_counts_from_json(obj["closure_counts"]),
            new_retained=obj["new_retained"],
            scrubbed_fids=tuple(obj["scrubbed_fids"]),
            report_ref=obj["report_ref"])
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def _safe_delete_proof_from_dict(obj) -> SafeDeleteProof:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        return SafeDeleteProof(
            closure_counts=_class_counts_from_json(obj["closure_counts"]),
            new_retained=obj["new_retained"],
            scrubbed_fids=tuple(obj["scrubbed_fids"]),
            report_ref=obj["report_ref"])
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def outcome_from_dict(obj) -> OperationOutcome:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        method = MethodCode(obj["method"])
    except (KeyError, ValueError):
        raise cap.ModelInputError()
    try:
        readback = obj.get("readback")
        return OperationOutcome(
            method=method, result_code=obj["result_code"],
            applied_at_revision=obj.get("applied_at_revision"),
            readback=(_readback_from_dict(readback)
                      if readback is not None else None),
            conflict_fields=tuple(ConflictField(f) for f in
                                  obj.get("conflict_fields", ())),
            superseded=tuple(obj.get("superseded", ())),
            collided_fid=obj.get("collided_fid"),
            new_epoch=obj.get("new_epoch"),
            proof=(_delete_proof_from_dict(obj["proof"])
                   if obj.get("proof") is not None else None),
            report=(_wipe_report_from_dict(obj["report"])
                    if obj.get("report") is not None else None),
            record=(_optin_record_from_dict(obj["record"])
                    if obj.get("record") is not None else None))
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def scrubbed_from_dict(obj) -> ScrubbedOutcome:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        method = MethodCode(obj["method"])
    except (KeyError, ValueError):
        raise cap.ModelInputError()
    try:
        return ScrubbedOutcome(
            method=method, result_code=obj["result_code"],
            applied_at_revision=obj.get("applied_at_revision"),
            conflict_fields=tuple(ConflictField(f) for f in
                                  obj.get("conflict_fields", ())),
            superseded=tuple(obj.get("superseded", ())),
            collided_fid=obj.get("collided_fid"),
            new_epoch=obj.get("new_epoch"),
            safe_proof=(_safe_delete_proof_from_dict(obj["safe_proof"])
                        if obj.get("safe_proof") is not None else None),
            safe_report=(_safe_wipe_report_from_dict(obj["report"])
                         if obj.get("report") is not None else None))
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


def scrub_operation_outcome(outcome: OperationOutcome) -> ScrubbedOutcome:
    """The structured post-erasure transform: content positions -> 'erased'
    literal; safe immutable fields retained; delete keeps its SAFE proof."""
    return ScrubbedOutcome(
        method=outcome.method, result_code=outcome.result_code,
        applied_at_revision=outcome.applied_at_revision,
        conflict_fields=outcome.conflict_fields,
        superseded=outcome.superseded, collided_fid=outcome.collided_fid,
        new_epoch=outcome.new_epoch,
        safe_proof=(SafeDeleteProof(
            closure_counts=outcome.proof.closure_counts,
            new_retained=outcome.proof.new_retained,
            scrubbed_fids=outcome.proof.scrubbed_fids,
            report_ref=outcome.proof.report_ref)
            if outcome.proof is not None else None),
        safe_report=(SafeWipeReport(
            owner=outcome.report.owner,
            operation_id=outcome.report.operation_id,
            new_epoch=outcome.report.new_epoch,
            counts=outcome.report.counts,
            new_retained=outcome.report.new_retained,
            commit_revision=outcome.report.commit_revision,
            scope=outcome.report.scope,
            report_id=outcome.report.report_id,
            page_count=outcome.report.page_count,
            retained=tuple(r.name for r in outcome.report.retained),
            unknown_coverage=outcome.report.unknown_coverage,
            external_revocations=outcome.report.external_revocations,
            future_derivation_state=
            outcome.report.future_derivation_state)
            if outcome.report is not None else None))


# ---------------------------------------------------------------------------
# Receipts (sec 7 - ONE Receipt shape; the outcome discriminator selects)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Receipt:
    scope: "env.ModelOnlyOperationScope"
    binding_hash: str
    operation_id: str
    owner: Lifetime
    fence: int
    store_revision: int
    outcome: str                    # 'committed'
    operation_outcome: object       # OperationOutcome | ScrubbedOutcome

    def __post_init__(self) -> None:
        if type(self.scope) is not env.ModelOnlyOperationScope:
            raise cap.ModelInputError()
        _req_hex(self.binding_hash, HASH_LEN)
        _req_hex(self.operation_id, OP_ID_LEN)
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.fence, 1, 1)
        _req_int(self.store_revision, 1)
        if self.outcome != "committed":
            raise cap.ModelInputError()
        if type(self.operation_outcome) not in (OperationOutcome,
                                                ScrubbedOutcome):
            raise cap.ModelInputError()

    def to_dict(self) -> dict:
        outcome = self.operation_outcome
        return {
            "binding_hash": self.binding_hash,
            "fence": self.fence,
            "operation_id": self.operation_id,
            "operation_outcome": outcome.to_dict(),
            "outcome": self.outcome,
            "outcome_kind": ("full" if type(outcome) is OperationOutcome
                             else "scrubbed"),
            "owner": {"lifetime_id": self.owner.lifetime_id,
                      "uid": self.owner.uid},
            "scope": {"lifetime": self.scope.lifetime,
                      "operation_id": self.scope.operation_id,
                      "uid": self.scope.uid},
            "store_revision": self.store_revision,
        }


def receipt_from_dict(obj) -> Receipt:
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        scope = obj["scope"]
        kind = obj["outcome_kind"]
        raw = obj["operation_outcome"]
        if kind == "full":
            outcome = outcome_from_dict(raw)
        elif kind == "scrubbed":
            outcome = scrubbed_from_dict(raw)
        else:
            raise cap.ModelInputError()
        return Receipt(
            scope=env.ModelOnlyOperationScope(
                uid=scope["uid"], lifetime=scope["lifetime"],
                operation_id=scope["operation_id"]),
            binding_hash=obj["binding_hash"],
            operation_id=obj["operation_id"],
            owner=Lifetime(uid=obj["owner"]["uid"],
                           lifetime_id=obj["owner"]["lifetime_id"]),
            fence=obj["fence"], store_revision=obj["store_revision"],
            outcome=obj["outcome"], operation_outcome=outcome)
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()


@dataclass(frozen=True)
class RolledBackReceipt:
    scope: "env.ModelOnlyOperationScope"
    binding_hash: str
    operation_id: str
    owner: Lifetime
    fence: int
    store_revision: Optional[int]   # None ONLY here
    outcome: str                    # 'rolled_back'
    operation_outcome: None = None

    def __post_init__(self) -> None:
        if type(self.scope) is not env.ModelOnlyOperationScope:
            raise cap.ModelInputError()
        _req_hex(self.binding_hash, HASH_LEN)
        _req_hex(self.operation_id, OP_ID_LEN)
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.fence, 1, 1)
        if self.store_revision is not None or self.outcome != "rolled_back" \
                or self.operation_outcome is not None:
            raise cap.ModelInputError()

    def to_dict(self) -> dict:
        return {
            "binding_hash": self.binding_hash,
            "fence": self.fence,
            "operation_id": self.operation_id,
            "operation_outcome": None,
            "outcome": self.outcome,
            "owner": {"lifetime_id": self.owner.lifetime_id,
                      "uid": self.owner.uid},
            "scope": {"lifetime": self.scope.lifetime,
                      "operation_id": self.scope.operation_id,
                      "uid": self.scope.uid},
            "store_revision": None,
        }


# ---------------------------------------------------------------------------
# Result unions (sec 7) - tagged dataclasses, kind discriminator
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BlockedRecord:
    recovery_id: str
    uid: int
    lifetime_id: int
    operation_id: str
    binding_hash: str
    proof: str
    resolver: ResolverCode
    deadline: int
    retention: RetentionCode
    state: str                    # blocked|resolving|resolved
    evidence_count: int
    overflow_marker: int
    created_at: int

    def __post_init__(self) -> None:
        _req_str(self.recovery_id, BOUNDED_STR_MAX)
        _req_int(self.uid, 0)
        _req_int(self.lifetime_id, 1)
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_hex(self.binding_hash, HASH_LEN)
        _req_str(self.proof, 4096)
        _req_enum(self.resolver, ResolverCode)
        _req_int(self.deadline, 0)
        _req_enum(self.retention, RetentionCode)
        if self.state not in ("blocked", "resolving", "resolved"):
            raise cap.ModelInputError()
        _req_int(self.evidence_count, 0, EVIDENCE_MAX_PER_OP)
        _req_int(self.overflow_marker, 0, 1)
        _req_int(self.created_at, 0)


@dataclass(frozen=True)
class BlockedStatus:
    status: str                   # durable_blocked|local_uncertain|in_progress|unknown
    record: Optional[BlockedRecord] = None
    kind: str = "blocked_status"

    def __post_init__(self) -> None:
        if self.status not in ("durable_blocked", "local_uncertain",
                               "in_progress", "unknown"):
            raise cap.ModelInputError()
        if self.record is not None and type(self.record) is not BlockedRecord:
            raise cap.ModelInputError()
        if self.kind != "blocked_status":
            raise cap.ModelInputError()


@dataclass(frozen=True)
class FactResult:
    owner: Lifetime
    operation_id: str
    outcome: OperationOutcome
    receipt: Receipt

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_hex(self.operation_id, OP_ID_LEN)
        if type(self.outcome) is not OperationOutcome:
            raise cap.ModelInputError()
        if type(self.receipt) is not Receipt:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class ReplayResult:
    kind: str                     # outcome|scrubbed
    outcome: object               # OperationOutcome | ScrubbedOutcome

    def __post_init__(self) -> None:
        if self.kind == "outcome":
            if type(self.outcome) is not OperationOutcome:
                raise cap.ModelInputError()
        elif self.kind == "scrubbed":
            if type(self.outcome) is not ScrubbedOutcome:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class GetResult:
    owner: Lifetime
    query_revision: int
    row: Optional[FactReadback]

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.query_revision, 1)
        if self.row is not None and type(self.row) is not FactReadback:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class OptInView:
    kind: str                     # stored|default_off
    record: Optional[OptinRecord] = None

    def __post_init__(self) -> None:
        if self.kind == "stored":
            if type(self.record) is not OptinRecord:
                raise cap.ModelInputError()
        elif self.kind == "default_off":
            if self.record is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class GetOptinResult:
    owner: Lifetime
    query_revision: int
    view: OptInView

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.query_revision, 1)
        if type(self.view) is not OptInView:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class CountFilters:
    include_superseded: bool
    category: Optional[Category] = None

    def __post_init__(self) -> None:
        _req_bool(self.include_superseded)
        if self.category is not None:
            _req_enum(self.category, Category)


@dataclass(frozen=True)
class CountResult:
    owner: Lifetime
    query_revision: int
    filters: CountFilters
    value: int

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.query_revision, 1)
        if type(self.filters) is not CountFilters:
            raise cap.ModelInputError()
        _req_int(self.value, 0)


@dataclass(frozen=True)
class FactPage:
    owner: Lifetime
    query_revision: int
    row_kind: RowKind
    rows: Tuple[object, ...]      # FactReadback | HistoryRow
    next_cursor: Optional[str]    # 32-hex opaque id | None
    bounded_by: str

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.query_revision, 1)
        _req_enum(self.row_kind, RowKind)
        rows = tuple(self.rows)
        expected = FactReadback if self.row_kind is RowKind.LIVE else HistoryRow
        for row in rows:
            if type(row) is not expected:
                raise cap.ModelInputError()
        object.__setattr__(self, "rows", rows)
        if self.next_cursor is not None:
            _req_hex(self.next_cursor, CURSOR_WIRE_LEN)
        _req_str(self.bounded_by, BOUNDED_STR_MAX)


# CURRENT-STATE VIEWS (sec 7 - FIELDED per-method views)
@dataclass(frozen=True)
class CurrentStateView:
    """UNION keyed by (view, state); DISJOINT field presence per member."""
    view: str                     # put|supersede|delete|wipe|optin
    state: str
    row: Optional[FactReadback] = None
    conflict_fields: Tuple[ConflictField, ...] = ()
    erased: Tuple[int, ...] = ()
    collided_fid: Optional[int] = None
    safe_report: Optional[SafeWipeReport] = None
    optin: Optional[OptInView] = None

    def __post_init__(self) -> None:
        allowed = {
            ("put", "live"), ("put", "erased"), ("put", "absent"),
            ("put", "no_op"),
            ("supersede", "live"), ("supersede", "collision"),
            ("delete", "erased"), ("delete", "absent"),
            ("wipe", "erased"),
        }
        if (self.view, self.state) not in allowed and self.view != "optin":
            raise cap.ModelInputError()
        if self.view == "optin" and type(self.optin) is not OptInView:
            raise cap.ModelInputError()
        if self.view != "optin" and self.optin is not None:
            raise cap.ModelInputError()
        fields = tuple(self.conflict_fields)
        for entry in fields:
            _req_enum(entry, ConflictField)
        if fields != tuple(sorted(set(fields), key=lambda f: f.value)):
            raise cap.ModelInputError()
        object.__setattr__(self, "conflict_fields", fields)
        erased = tuple(self.erased)
        for fid in erased:
            _req_int(fid, 1)
        object.__setattr__(self, "erased", erased)
        if self.collided_fid is not None:
            _req_int(self.collided_fid, 1)
        if self.row is not None and type(self.row) is not FactReadback:
            raise cap.ModelInputError()
        if self.safe_report is not None \
                and type(self.safe_report) is not SafeWipeReport:
            raise cap.ModelInputError()
        # variant coherence
        if self.state == "live":
            if type(self.row) is not FactReadback:
                raise cap.ModelInputError()
            if self.view == "supersede" and not erased:
                pass  # erased list may be empty in degenerate fixture views
        # no_op: unchanged carries EMPTY conflict_fields; conflict carries
        # the SORTED differing fields - both are DECLARED
        if self.state == "collision" and self.collided_fid is None:
            raise cap.ModelInputError()
        if self.view == "wipe" and self.state == "erased" \
                and type(self.safe_report) is not SafeWipeReport:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class CurrentResult:
    kind: str                     # available|moved|absent|unavailable
    view: Optional[CurrentStateView] = None
    currently_held: Optional[CurrentStateView] = None
    cause: Optional[ReadbackCause] = None

    def __post_init__(self) -> None:
        if self.kind == "available":
            if type(self.view) is not CurrentStateView \
                    or self.currently_held is not None or self.cause is not None:
                raise cap.ModelInputError()
        elif self.kind == "moved":
            if type(self.currently_held) is not CurrentStateView \
                    or self.view is not None or self.cause is not None:
                raise cap.ModelInputError()
        elif self.kind == "absent":
            if self.view is not None or self.currently_held is not None \
                    or self.cause is not None:
                raise cap.ModelInputError()
        elif self.kind == "unavailable":
            _req_enum(self.cause, ReadbackCause)
            if self.view is not None or self.currently_held is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class PostReadResult:
    original_outcome: object      # StoredOutcome (full or scrubbed)
    applied_at_revision: Optional[int]
    current_revision: Optional[int]
    current: CurrentResult

    def __post_init__(self) -> None:
        if type(self.original_outcome) not in (OperationOutcome,
                                               ScrubbedOutcome):
            raise cap.ModelInputError()
        if self.applied_at_revision is not None:
            _req_int(self.applied_at_revision, 1)
        if self.current_revision is not None:
            _req_int(self.current_revision, 1)
        if type(self.current) is not CurrentResult:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class ExecuteResult:
    """The ACTUAL execute return (sec 7 closed union)."""
    kind: str                     # executed|replayed|durable_blocked|local_uncertain|in_progress|unknown
    result: Optional[FactResult] = None
    post: Optional[PostReadResult] = None
    replay: Optional[ReplayResult] = None
    record: Optional[BlockedRecord] = None
    operation_id: Optional[str] = None
    owner: Optional[Lifetime] = None
    binding_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind == "executed":
            if type(self.result) is not FactResult \
                    or type(self.post) is not PostReadResult:
                raise cap.ModelInputError()
        elif self.kind == "replayed":
            if type(self.replay) is not ReplayResult \
                    or self.result is not None or self.post is not None:
                raise cap.ModelInputError()
        elif self.kind == "durable_blocked":
            if type(self.record) is not BlockedRecord:
                raise cap.ModelInputError()
        elif self.kind == "local_uncertain":
            if self.operation_id is None or self.owner is None \
                    or self.binding_hash is None:
                raise cap.ModelInputError()
            _req_hex(self.operation_id, OP_ID_LEN)
            if type(self.owner) is not Lifetime:
                raise cap.ModelInputError()
            _req_hex(self.binding_hash, HASH_LEN)
        elif self.kind in ("in_progress", "unknown"):
            pass
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class ResolutionResult:
    """The resolve return (sec 7 closed union)."""
    kind: str                     # committed|rolled_back|durable_blocked|local_uncertain
    post: Optional[PostReadResult] = None
    receipt: Optional[RolledBackReceipt] = None
    record: Optional[BlockedRecord] = None
    resolution_event_id: Optional[str] = None
    resolved_count: Optional[int] = None
    attested_witness_count: Optional[int] = None
    new_retained: Optional[int] = None
    operation_id: Optional[str] = None
    owner: Optional[Lifetime] = None
    original_binding: Optional["env.ModelOnlyRequestBinding"] = None

    def __post_init__(self) -> None:
        if self.kind == "committed":
            if type(self.post) is not PostReadResult:
                raise cap.ModelInputError()
        elif self.kind == "rolled_back":
            if type(self.receipt) is not RolledBackReceipt:
                raise cap.ModelInputError()
        elif self.kind == "durable_blocked":
            if type(self.record) is not BlockedRecord:
                raise cap.ModelInputError()
            return
        elif self.kind == "local_uncertain":
            if self.operation_id is None or self.owner is None \
                    or self.original_binding is None:
                raise cap.ModelInputError()
            _req_hex(self.operation_id, OP_ID_LEN)
            if type(self.owner) is not Lifetime:
                raise cap.ModelInputError()
            if type(self.original_binding) is not env.ModelOnlyRequestBinding:
                raise cap.ModelInputError()
            return
        else:
            raise cap.ModelInputError()
        # resolved kinds carry the ACTUAL resolver accounting fields
        if self.resolution_event_id is None:
            raise cap.ModelInputError()
        _req_str(self.resolution_event_id, BOUNDED_STR_MAX)
        _req_int(self.resolved_count, 0)
        _req_int(self.attested_witness_count, 0)
        _req_int(self.new_retained, 0)


@dataclass(frozen=True)
class PageUnavailable:
    """read_report_page's unavailable branch (sec 13)."""
    kind: str                     # erased|unknown
    report_id: Optional[str] = None
    owner: Optional[Lifetime] = None
    operation_id: Optional[str] = None
    commit_revision: Optional[int] = None

    def __post_init__(self) -> None:
        if self.kind == "erased":
            if self.report_id is None or self.owner is None \
                    or self.operation_id is None \
                    or self.commit_revision is None:
                raise cap.ModelInputError()
            _req_str(self.report_id, BOUNDED_STR_MAX)
            if type(self.owner) is not Lifetime:
                raise cap.ModelInputError()
            _req_hex(self.operation_id, OP_ID_LEN)
            _req_int(self.commit_revision, 1)
        elif self.kind == "unknown":
            if self.report_id is not None or self.owner is not None \
                    or self.operation_id is not None \
                    or self.commit_revision is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


# ---------------------------------------------------------------------------
# ClosureEntry (sec 13 CLOSED DISJOINT SUM) + ExpectedClosureManifest
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ClosureEntry:
    """The ONE disjoint closure entry sum (sec 13). CANONICAL IDENTITY:
    (uid, lifetime_id, class_name, op_form, entry_id) with op_form = None
    for every non-op_records class."""
    class_name: str               # facts|history|op_records|stored_requests|copy class|abort_witnesses|insertion_events|resolution_event_ledger|attempt_registry|resolved_accounting|report_pages
    entry_id: str
    state: str
    version: int
    op_form: Optional[str] = None       # record|receipt for op_records ONLY
    op_id: Optional[str] = None
    receipt_ref: Optional[str] = None
    association: Optional[dict] = None  # {copy_class, entry_id} NON-NULL link
    resolution: Optional[str] = None    # committed|rolled_back (resolved only)
    ids_digest: Optional[str] = None    # PageEntry only

    def __post_init__(self) -> None:
        _classes = {c.value for c in CopyClass} | {"op_records"}
        if self.class_name not in _classes:
            raise cap.ModelInputError()
        _req_str(self.entry_id, BOUNDED_STR_MAX)
        _req_str(self.state, 32)
        _req_int(self.version, 1)
        if self.class_name == "op_records":
            if self.op_form not in ("record", "receipt"):
                raise cap.ModelInputError()
        elif self.op_form is not None:
            raise cap.ModelInputError()
        if self.op_id is not None:
            _req_hex(self.op_id, OP_ID_LEN)
        if self.receipt_ref is not None:
            _req_str(self.receipt_ref, BOUNDED_STR_MAX)
        if self.association is not None:
            if type(self.association) is not dict:
                raise cap.ModelInputError()
            try:
                CopyClass(self.association["copy_class"])
            except (KeyError, ValueError):
                raise cap.ModelInputError()
            _req_str(self.association.get("entry_id"), BOUNDED_STR_MAX)
        if self.resolution is not None \
                and self.resolution not in ("committed", "rolled_back"):
            raise cap.ModelInputError()
        if self.ids_digest is not None:
            _req_hex(self.ids_digest, HASH_LEN)

    def identity(self, uid: int, lifetime_id: int) -> tuple:
        return (uid, lifetime_id, self.class_name, self.op_form,
                self.entry_id)

    def to_dict(self, uid: int, lifetime_id: int) -> dict:
        out = {
            "association": self.association,
            "class_name": self.class_name,
            "entry_id": self.entry_id,
            "ids_digest": self.ids_digest,
            "lifetime_id": lifetime_id,
            "op_form": self.op_form,
            "op_id": self.op_id,
            "receipt_ref": self.receipt_ref,
            "resolution": self.resolution,
            "state": self.state,
            "uid": uid,
            "version": self.version,
        }
        return out


@dataclass(frozen=True)
class ExpectedClosureManifest:
    owner: Lifetime
    fid: int
    live: Optional[int]
    entries: Tuple[ClosureEntry, ...]
    ops_digest: str
    copies_digest: str

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_int(self.fid, 1)
        if self.live is not None:
            _req_int(self.live, 1)
        entries = tuple(self.entries)
        seen = set()
        for entry in entries:
            if type(entry) is not ClosureEntry:
                raise cap.ModelInputError()
            ident = entry.identity(self.owner.uid, self.owner.lifetime_id)
            if ident in seen:
                raise cap.ModelInputError()  # duplicate = integrity error
            seen.add(ident)
        if entries != tuple(sorted(entries, key=lambda e: e.identity(
                self.owner.uid, self.owner.lifetime_id))):
            raise cap.ModelInputError()
        object.__setattr__(self, "entries", entries)
        _req_hex(self.ops_digest, HASH_LEN)
        _req_hex(self.copies_digest, HASH_LEN)

    def manifest_dict(self) -> dict:
        return {
            "copies_digest": self.copies_digest,
            "entries": [e.to_dict(self.owner.uid, self.owner.lifetime_id)
                        for e in self.entries],
            "fid": self.fid,
            "live": self.live,
            "ops_digest": self.ops_digest,
            "owner": {"lifetime_id": self.owner.lifetime_id,
                      "uid": self.owner.uid},
        }

    def lineage_digest(self) -> str:
        """SHA-256 over canonical JSON of {owner, fid, live, entries,
        ops_digest, copies_digest} - the WHOLE manifest (sec 7)."""
        return _sha256_hex(_canon(self.manifest_dict()))


def lineage_ops_digest(rows) -> str:
    """ops_digest: SHA-256 over canonical sorted array of
    {uid, lifetime_id, fact_id, seq, op_id} rows (PK order, unique)."""
    canon_rows = sorted(
        ({"fact_id": int(r[0]), "lifetime_id": int(r[1]),
          "op_id": r[2], "seq": int(r[3]), "uid": int(r[4])}
         for r in rows),
        key=lambda d: (d["uid"], d["lifetime_id"], d["fact_id"], d["seq"]))
    if len({(d["uid"], d["lifetime_id"], d["fact_id"], d["seq"])
            for d in canon_rows}) != len(canon_rows):
        raise cap.ModelInputError()  # duplicate row = integrity reject
    return _sha256_hex(_canon(canon_rows))


def lineage_copies_digest(rows) -> str:
    canon_rows = sorted(
        ({"copy_id": r[0], "fact_id": int(r[1]),
          "lifetime_id": int(r[2]), "uid": int(r[3])} for r in rows),
        key=lambda d: (d["uid"], d["lifetime_id"], d["fact_id"],
                       d["copy_id"]))
    if len({(d["uid"], d["lifetime_id"], d["fact_id"], d["copy_id"])
            for d in canon_rows}) != len(canon_rows):
        raise cap.ModelInputError()
    return _sha256_hex(_canon(canon_rows))


def page_ids_digest(status: str, ids_json: Optional[str]) -> str:
    """The ONE tagged rule: active page digests UTF8('active:'+canonical IDs
    JSON); an erased page (ids_json NULL) digests UTF8('erased:null')."""
    if status == "active":
        if type(ids_json) is not str:
            raise cap.ModelInputError()
        return _sha256_hex(("active:" + ids_json).encode("utf-8"))
    if status == "erased":
        if ids_json is not None:
            raise cap.ModelInputError()
        return _sha256_hex(b"erased:null")
    raise cap.ModelInputError()


# ---------------------------------------------------------------------------
# HistoricalTarget (sec 13 CLOSED SUM - the discriminant IS the shape)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class SuppliedMode:
    recomputed_commitment: str    # the store-RECOMPUTED extended hash
    mode: str = "SUPPLIED"

    def __post_init__(self) -> None:
        _req_hex(self.recomputed_commitment, HASH_LEN)
        if self.mode != "SUPPLIED":
            raise cap.ModelInputError()


@dataclass(frozen=True)
class LoadStoredMode:
    operation_id: str
    original_binding: "env.ModelOnlyRequestBinding"
    mode: str = "LOAD_STORED"

    def __post_init__(self) -> None:
        _req_hex(self.operation_id, OP_ID_LEN)
        if type(self.original_binding) is not env.ModelOnlyRequestBinding:
            raise cap.ModelInputError()
        if self.mode != "LOAD_STORED":
            raise cap.ModelInputError()


HistoricalTarget = (SuppliedMode, LoadStoredMode)


# ---------------------------------------------------------------------------
# SessionBound wrappers (sec 4 PROCESS BOUNDARY - the ONE TYPED bundle)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class _SessionBound:
    session_id: str
    handle: str
    inner: object
    binding_hash: str

    def __post_init__(self) -> None:
        _req_hex(self.session_id, 32)
        _req_hex(self.handle, 32)
        _req_hex(self.binding_hash, HASH_LEN)


@dataclass(frozen=True)
class SessionBoundCapability(_SessionBound):
    inner: "cap.Capability"


@dataclass(frozen=True)
class SessionBoundClaim(_SessionBound):
    inner: "env.ModelOnlyClaimResult"


@dataclass(frozen=True)
class SessionBoundCapture(_SessionBound):
    inner: "env.ModelOnlyVerifiedCapture"


# ---------------------------------------------------------------------------
# StoreClock + JsonTestClock (sec 13 - epoch SECONDS everywhere)
# ---------------------------------------------------------------------------

class StoreClock(Protocol):
    def now(self) -> int: ...  # epoch seconds; validated at every read


class JsonTestClock:
    """JSON-driven deterministic clock. TEST-ONLY fixture."""

    def __init__(self, start: int = 1_700_000_000) -> None:
        _req_int(start, 0)
        self._now = start

    def now(self) -> int:
        return self._now

    def advance(self, seconds: int) -> None:
        _req_int(seconds, 0)
        self._now = _checked_add(self._now, seconds)


def _write_tombstone(q, uid, lid, norm_key, generation, now):
    """Erasure marker write (sec 5): content-free HMAC key_ref under the
    owner's fixture key; generation = the epoch that erased it."""
    key_row = q.q("SELECT fixture_key FROM marker_keys WHERE uid=%s AND"
                  " lifetime_id=%s", (uid, lid), "one")
    if key_row is None:
        raise cap.ModelInputError()
    key_ref = _hmac.new(key_row["fixture_key"].encode("utf-8"),
                        norm_key.encode("utf-8"),
                        hashlib.sha256).hexdigest()
    q.q("INSERT INTO markers(marker_key, uid, lifetime_id, marker_class,"
        " key_ref, generation, created_at)"
        " VALUES(%s,%s,%s,%s,%s,%s,%s)",
        (key_row["fixture_key"], uid, lid, "tombstones", key_ref,
         generation, now), "exec")



def _recovery_id(uid: int, lid: int, op_id: str, commitment: str) -> str:
    """The FIXTURE-issued recovery id (sec 5): a deterministic 32-hex
    derivation from the operation identity + its commitment."""
    return _sha256_hex(_canon({"uid": uid, "lifetime_id": lid,
                               "operation_id": op_id,
                               "commitment": commitment}))



def _clock_now(clock) -> int:
    if not callable(getattr(clock, "now", None)):
        raise cap.ModelInputError()
    value = clock.now()
    _req_int(value, 0)
    return value


# ---------------------------------------------------------------------------
# FactStoreSnapshotStore (sec 13 - NEW LOCAL implementation of the PR-3
# ModelOnlySnapshotStore PROTOCOL over the OWNED fixture tables, SAME
# connection, INSIDE the transaction - the atomic current re-read is real)
# ---------------------------------------------------------------------------

class FactStoreSnapshotStore:
    def __init__(self, queryable: "_Queryable") -> None:
        if type(queryable) is not _Queryable:
            raise cap.ModelInputError()
        self._q = queryable

    def read(self, owner_uid: int) -> "cap.Snapshot":
        _req_int(owner_uid, 0)
        q = self._q
        snap = q.q("SELECT * FROM fixture_snapshot WHERE uid=%s",
                   (owner_uid,), "one")
        if snap is None:
            raise cap.AuthError()
        grants = tuple(
            cap.Grant(scope=cap.Scope(row["scope"]),
                      granted_at=row["granted_at"],
                      expires_at=row["expires_at"],
                      per_op_required=bool(row["per_op_required"]))
            for row in q.q("SELECT * FROM fixture_grants WHERE uid=%s"
                           " ORDER BY scope", (owner_uid,), "all"))
        consents = tuple(
            cap.OperationConsent(
                owner_uid=owner_uid, operation_id=row["operation_id"],
                operation_class=cap.OperationClass(row["operation_class"]),
                scopes=frozenset(cap.Scope(s)
                                 for s in row["scope"].split(",")),
                audience_id=row["audience_id"],
                permitted_information_ref=row["permitted_information_ref"],
                issued_at=row["issued_at"], expires_at=row["expires_at"],
                consent_generation=row["consent_generation"])
            for row in q.q("SELECT * FROM fixture_consents WHERE uid=%s"
                           " ORDER BY operation_id", (owner_uid,), "all"))
        policy_row = q.q("SELECT * FROM fixture_policy WHERE uid=%s",
                         (owner_uid,), "one")
        config = None
        if policy_row is not None:
            config = cap.Policy(
                extraction=cap.ExtractionPolicy(policy_row["extraction"]),
                sharing=cap.SharingPolicy(policy_row["sharing"]),
                group_write=cap.GroupEligibility(policy_row["group_write"]))
        env_row = q.q("SELECT * FROM fixture_envelope WHERE uid=%s",
                      (owner_uid,), "one")
        envelope = None
        if env_row is not None:
            envelope = cap.DecisionEnvelope(
                owner_uid=owner_uid,
                policy=cap.Policy(
                    extraction=cap.ExtractionPolicy(env_row["extraction"]),
                    sharing=cap.SharingPolicy(env_row["sharing"]),
                    group_write=cap.GroupEligibility(env_row["group_write"])),
                issued_at=env_row["issued_at"],
                expires_at=env_row["expires_at"],
                revision=env_row["revision"])
        return cap.Snapshot(
            owner_uid=owner_uid, lifetime_id=snap["lifetime_id"],
            current_epoch=snap["current_epoch"],
            grants_generation=snap["grants_generation"], grants=grants,
            policy_revision=snap["policy_revision"],
            decision_revision=snap["decision_revision"], config=config,
            decision_envelope=envelope,
            consent_generation=snap["consent_generation"], consents=consents)


# ---------------------------------------------------------------------------
# Synthetic verifier fixture (tier-0 identity verification is PR-3's; the
# proof is synthetic, never authority)
# ---------------------------------------------------------------------------

class SyntheticProofVerifier:
    __test__ = False

    def verify(self, model_proof: "cap.ModelProof") -> "cap.Identity":
        if type(model_proof) is not cap.ModelProof:
            raise cap.AuthError()
        return model_proof.identity


# ---------------------------------------------------------------------------
# Producer fixtures (sec 13 TYPED PRODUCER SETUPS - the REAL PR-3/PR-4
# composition). The producer mints typed cap/claim/capture fixtures; the
# store NEVER registers an arbitrary external object.
# ---------------------------------------------------------------------------

_METHOD_OPERATION = {
    MethodCode.PUT: cap.OperationClass.WRITE,
    MethodCode.SUPERSEDE: cap.OperationClass.WRITE,
    MethodCode.SET_OPTIN: cap.OperationClass.WRITE,
    MethodCode.DELETE: cap.OperationClass.ERASE,
    MethodCode.WIPE: cap.OperationClass.ERASE,
}

_METHOD_SCOPES = {
    MethodCode.PUT: frozenset({cap.Scope.MEMORY_WRITE}),
    MethodCode.SUPERSEDE: frozenset({cap.Scope.MEMORY_WRITE}),
    MethodCode.SET_OPTIN: frozenset({cap.Scope.MEMORY_WRITE}),
    MethodCode.DELETE: frozenset({cap.Scope.MEMORY_ERASE}),
    MethodCode.WIPE: frozenset({cap.Scope.MEMORY_ERASE}),
}


class _StoreProducer:
    """Store-OWNED fixture producer (sec 13): mints the READ and MUTATION
    proof/request/context fixtures from the ACTUAL unchanged PR-3 mint +
    recheck and PR-4 verify/claim composition. NO caller-supplied
    identity/lifetime/scopes/times/revisions/state/token/fence."""

    def __init__(self, store: "FactStore") -> None:
        self._store = store

    def _request_for(self, operation_id: str,
                     operation_class: "cap.OperationClass",
                     scopes, captured_epoch: Optional[int],
                     information_ref: str) -> "cap.Request":
        return cap.Request(
            operation_id=operation_id, operation_class=operation_class,
            required_scopes=frozenset(scopes), captured_epoch=captured_epoch,
            requested_audience_id=None,
            permitted_information_ref=information_ref,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            group_action=None, sensitive=False)

    def _identity(self, owner_uid: int, lifetime_id: int,
                  channel: "cap.Channel") -> "cap.Identity":
        issuer_id = ("session:" + secrets.token_hex(32)
                     if channel is cap.Channel.WEB
                     else "telegram:" + secrets.token_hex(16))
        return cap.Identity(
            owner_uid=owner_uid, channel=channel, surface=cap.Surface.PRIVATE,
            issuer_id=issuer_id, session_binding=None, audience_id=None,
            lifetime_id=lifetime_id, origin=cap.Origin.DIRECT)

    def mint_read(self, owner_uid: int, lifetime_id: int,
                  operation_id: str,
                  method: ReadMethod) -> "cap.Capability":
        """The READ setup: mints the READ proof/request fixtures via the REAL
        PR-3 mint (current snapshot + clock path)."""
        _req_enum(method, ReadMethod)
        snapshot = self._store._snapshots.read(owner_uid)
        if snapshot.lifetime_id != lifetime_id:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        request = self._request_for(operation_id, cap.OperationClass.READ,
                                    {cap.Scope.MEMORY_READ}, None,
                                    "read:" + method.value)
        proof = cap.ModelProof(
            identity=self._identity(owner_uid, lifetime_id,
                                    cap.Channel.WEB),
            proof_label="pr5-read")
        return self._store._evaluator.mint_model(proof, request)

    def mint_mutation(self, request: FactRequest) -> "cap.Capability":
        """The MUTATION setup: mints the FactRequest capability fixture via
        the REAL PR-3 mint against the CURRENT snapshot."""
        snapshot = self._store._snapshots.read(request.owner_uid)
        if snapshot.lifetime_id != request.lifetime_id:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        cap_request = self._request_for(
            request.operation_id, _METHOD_OPERATION[request.method],
            _METHOD_SCOPES[request.method], snapshot.current_epoch,
            request.information_ref)
        proof = cap.ModelProof(
            identity=self._identity(request.owner_uid, request.lifetime_id,
                                    cap.Channel.WEB),
            proof_label="pr5-mutation")
        return self._store._evaluator.mint_model(proof, cap_request)

    def build_binding(self, request: FactRequest,
                      capability: "cap.Capability",
                      channel: "cap.Channel" = cap.Channel.WEB,
                      issuer=None,
                      expiry: Optional[int] = None) -> "env.ModelOnlyRequestBinding":
        """The MUTATION binding fixture, mapped per sec 4. NO target ids on
        the binding itself; NO nonce/fence fields exist on it."""
        snapshot = self._store._snapshots.read(request.owner_uid)
        now = _clock_now(self._store._clock)
        if issuer is None:
            issuer = self._store._stored_issuer(request.owner_uid)
            if issuer is None:
                issuer = env.SessionRef(
                    session_token_hash=secrets.token_hex(32))
        capture_time = now
        if expiry is None:
            # PR-4 immutable submitted expiry: capture_time + MAX_AGE
            expiry = _checked_add(capture_time, env.MAX_AGE_SECONDS)
        return env.ModelOnlyRequestBinding(
            uid=request.owner_uid, lifetime=request.lifetime_id,
            channel=channel, surface=cap.Surface.PRIVATE, issuer=issuer,
            operation_id=request.operation_id,
            operation_class=_METHOD_OPERATION[request.method],
            required_scopes=tuple(sorted(_METHOD_SCOPES[request.method],
                                         key=lambda s: s.value)),
            audience=env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                           id=None),
            information_ref=request.information_ref,
            content_fingerprint=_content_fingerprint(request),
            capture_epoch=snapshot.current_epoch,
            grant_generation=snapshot.grants_generation,
            policy_revision=snapshot.policy_revision,
            decision_revision=snapshot.decision_revision,
            consent_generation=snapshot.consent_generation,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            group_action=None, sensitive=False, origin=cap.Origin.DIRECT,
            capture_time=capture_time, expiry=expiry)

    def build_read_binding(self, owner_uid: int, lifetime_id: int,
                           operation_id: str,
                           method: "ReadMethod") -> "env.ModelOnlyRequestBinding":
        """The READ binding fixture (sec 13): the store's OWNED current
        issuer/snapshot context source - never caller-asserted."""
        snapshot = self._store._snapshots.read(owner_uid)
        now = _clock_now(self._store._clock)
        issuer = self._store._stored_issuer(owner_uid)
        if issuer is None:
            issuer = env.SessionRef(session_token_hash=secrets.token_hex(32))
        return env.ModelOnlyRequestBinding(
            uid=owner_uid, lifetime=lifetime_id, channel=cap.Channel.WEB,
            surface=cap.Surface.PRIVATE, issuer=issuer,
            operation_id=operation_id,
            operation_class=cap.OperationClass.READ,
            required_scopes=(cap.Scope.MEMORY_READ,),
            audience=env.ModelOnlyAudience(kind=env.AudienceKind.PRIVATE,
                                           id=None),
            information_ref="read:" + method.value,
            content_fingerprint=_sha256_hex(_canon(
                {"method": method.value, "operation_id": operation_id})),
            capture_epoch=snapshot.current_epoch,
            grant_generation=snapshot.grants_generation,
            policy_revision=snapshot.policy_revision,
            decision_revision=snapshot.decision_revision,
            consent_generation=snapshot.consent_generation,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            group_action=None, sensitive=False, origin=cap.Origin.DIRECT,
            capture_time=now, expiry=_checked_add(now, env.MAX_AGE_SECONDS))

    def seal_and_claim(self, binding: "env.ModelOnlyRequestBinding"):
        """The CLAIM setup: the REAL PR-4 verify_model + claim_model
        composition. Returns (capture, claim)."""
        store = self._store
        now = _clock_now(store._clock)
        key = store._keyring.lookup("k1", now)
        payload = env.ModelOnlyCapturedPayload(
            binding=binding, nonce=secrets.token_hex(16), format_version=1,
            key_id="k1")
        envelope = env.seal_model(payload, key, store._env_clock)
        capture = env.verify_model(envelope, store._keyring,
                                   store._caller_context(binding), store._env_clock)
        claim = store._registry.claim_model(capture, "owner")
        return capture, claim


# ---------------------------------------------------------------------------
# ReceiverContextProvider (test-only fixture; the caller NEVER mints tokens)
# ---------------------------------------------------------------------------

class ReceiverContextProvider:
    """The INJECTED private typed receiver callback (sec 5). EVERY method
    FIRST obtains a FRESH REGISTERED READ setup BOUND TO ITS ACTUAL INPUT
    (minted via the store's producer, NEVER a raw handle) -> handle
    checks + PhaseA + the CURRENT source/anchor read; NOTHING cached
    across calls or stages. Every refusal reason a CLOSED enum."""
    __test__ = False

    def __init__(self, store: "FactStore",
                 outbox_conn: sqlite3.Connection,
                 owner_uid: int, lifetime_id: int,
                 operation_id: str, clock: StoreClock) -> None:
        _req_int(owner_uid, 0)
        _req_int(lifetime_id, 1)
        _req_hex(operation_id, OP_ID_LEN)
        if type(outbox_conn) is not sqlite3.Connection:
            raise cap.ModelInputError()
        self._store = store          # the receiver FactStore (producer)
        self._outbox = outbox_conn   # the ISSUER-OWNED outbox (read-only)
        self.owner_uid = owner_uid
        self.lifetime_id = lifetime_id
        self.operation_id = operation_id
        self._clock = clock
        # test fault hooks (fixture-only): a lost ack / unreadable reread
        self._fault_no_ack = False
        self._fault_io_unreadable = False

    # -- the ONE source-bound read route ------------------------------------

    def _fresh_setup(self, targets: dict):
        """A FRESH REGISTERED READ setup bound to the ACTUAL input."""
        return self._store.mint_read_fixtures(
            self.owner_uid, self.lifetime_id, self.operation_id,
            ReadMethod.READBACK, targets)

    def _anchor_read(self, recovery_id: str) -> Optional[VerifiedAnchor]:
        """The VALIDATED CURRENT anchor via the accepted recovery-anchor
        JOIN: a resolved_accounting row = ResolvedAnchor; ELSE a CURRENT
        blocked_records row = BlockedAnchor (resolved proof NULL). Runs
        INSIDE the caller's read transaction."""
        q = self._store._q
        row = q.q("SELECT * FROM resolved_accounting WHERE recovery_id=%s",
                  (recovery_id,), "one")
        if row is not None:
            src = json.loads(row["source_json"])
            return VerifiedAnchor(
                recovery_id=recovery_id, anchor_kind="ResolvedAnchor",
                anchor_row=AnchorRow(uid=row["uid"],
                                     lifetime_id=row["lifetime_id"],
                                     operation_id=row["operation_id"]),
                commitment=row["commitment"],
                source=AnchorSource(
                    table="resolved_accounting", row_key=recovery_id,
                    resolved=ResolvedSource(
                        anchor=src["anchor"], commitment=src["commitment"],
                        operation_id=src["operation_id"],
                        resolver=src["resolver"])),
                validated_at=_clock_now(self._clock))
        row = q.q("SELECT * FROM blocked_records WHERE recovery_id=%s",
                  (recovery_id,), "one")
        if row is None or row["state"] == "resolved":
            return None
        return VerifiedAnchor(
            recovery_id=recovery_id, anchor_kind="BlockedAnchor",
            anchor_row=AnchorRow(uid=row["uid"],
                                 lifetime_id=row["lifetime_id"],
                                 operation_id=row["operation_id"]),
            commitment=row["binding_hash"],
            source=AnchorSource(table="blocked_records",
                                row_key=recovery_id, resolved=None),
            validated_at=_clock_now(self._clock))

    def _run_route(self, targets: dict):
        """handle checks + PhaseA + the CURRENT issuer INSIDE the one
        read transaction. Returns None on success or the CLOSED
        AnchorRefusal."""
        capability, context = self._fresh_setup(targets)
        try:
            self._store._read_prologue(self.owner_uid, capability,
                                       ReadMethod.READBACK, targets,
                                       context)
        except cap.AuthError:
            return None, None, AnchorRefusal.HANDLE_INVALID
        except cap.ConflictError as exc:
            if exc.args and exc.args[0] == cap.ModelCode.STALE_POLICY:
                return None, None, AnchorRefusal.STALE_ANCHOR
            return None, None, AnchorRefusal.PHASEA_FAILED
        except Exception:
            return None, None, AnchorRefusal.PHASEA_FAILED
        return capability, context, None

    # -- the FIVE declared methods ------------------------------------------

    def get(self, target: dict) -> dict:
        """get(target: {recovery_id}) -> verified/refused THROUGH the
        wrapper chain (a fresh read wrapper minted for the target)."""
        if type(target) is not dict or set(target) != {"recovery_id"}:
            raise cap.ModelInputError()
        recovery_id = target["recovery_id"]
        _req_str(recovery_id, BOUNDED_STR_MAX)
        targets = {"recovery_id": recovery_id}
        capability, context, refusal = self._run_route(targets)
        if refusal is not None:
            return {"status": "refused", "reason": refusal}
        try:
            anchor = self._anchor_read(recovery_id)
            self._store._read_commit()
        except Exception:
            self._store._read_abort()
            raise
        if anchor is None:
            return {"status": "refused", "reason": AnchorRefusal.UNKNOWN_RECOVERY}
        return {"status": "verified", "capability": capability,
                "context": context}

    def verify_anchor(self, input: dict) -> dict:
        """verify_anchor({recovery_id, projection}): the NAMED
        target-bound anchor method - registered setup -> handle checks
        -> PhaseA -> the scoped CURRENT anchor."""
        if type(input) is not dict \
                or set(input) != {"recovery_id", "projection"}:
            raise cap.ModelInputError()
        recovery_id = input["recovery_id"]
        projection = input["projection"]
        _req_str(recovery_id, BOUNDED_STR_MAX)
        if type(projection) is not SafeEvidenceProjection:
            raise cap.ModelInputError()
        if projection.recovery_id != recovery_id:
            return {"status": "refused",
                    "reason": AnchorRefusal.PROJECTION_MISMATCH}
        targets = {"projection": projection.to_dict(),
                   "recovery_id": recovery_id}
        _, _, refusal = self._run_route(targets)
        if refusal is not None:
            return {"status": "refused", "reason": refusal}
        try:
            anchor = self._anchor_read(recovery_id)
            self._store._read_commit()
        except Exception:
            self._store._read_abort()
            raise
        if anchor is None:
            return {"status": "refused", "reason": AnchorRefusal.UNKNOWN_RECOVERY}
        # the anchor's owner/operation/commitment MATCH the projection
        if anchor.anchor_row.uid != projection.uid \
                or anchor.anchor_row.lifetime_id != projection.lifetime_id \
                or anchor.anchor_row.operation_id != projection.operation_id \
                or anchor.commitment != projection.commitment:
            return {"status": "refused",
                    "reason": AnchorRefusal.PROJECTION_MISMATCH}
        # STORE-BUILT target/source binding (v80 795-812): the SAFE
        # target/source must EQUAL the subject's verified association
        if not _evidence_target_source_ok(
                self._store._q, projection, anchor.anchor_row.uid,
                anchor.anchor_row.lifetime_id,
                anchor.anchor_row.operation_id):
            return {"status": "refused",
                    "reason": AnchorRefusal.PROJECTION_MISMATCH}
        return {"status": "verified", "anchor": anchor}

    def deliver(self, input: dict) -> dict:
        """deliver({token, projection}) -> the DISJOINT three-way sum.
        The target recovery_id is DERIVED from the ISSUER OUTBOX ROW
        keyed by the token, NEVER from the caller."""
        if type(input) is not dict or set(input) != {"token", "projection"}:
            raise cap.ModelInputError()
        token = input["token"]
        projection = input["projection"]
        _req_hex(token, 32)
        if type(projection) is not SafeEvidenceProjection:
            raise cap.ModelInputError()
        try:
            row = self._outbox.execute(
                "SELECT recovery_id, projection_json FROM outbox"
                " WHERE token=?", (token,)).fetchone()
        except sqlite3.Error:
            return {"status": "refused", "reason": DeliveryRefusal.IO_FAILURE}
        if row is None:
            return {"status": "refused",
                    "reason": DeliveryRefusal.UNCONFIRMED_COMMIT}
        outbox_recovery = row[0]
        # TRANSPORT CHECK (i) BEFORE setup: the wire projection's
        # recovery_id EQUALS the outbox row's recovery_id
        if projection.recovery_id != outbox_recovery:
            return {"status": "refused",
                    "reason": DeliveryRefusal.WIRE_RECOVERY_MISMATCH}
        stored = SafeEvidenceProjection.from_dict(json.loads(row[1]))
        # the delivered projection MUST EQUAL the stored SAFE projection
        if projection.to_dict() != stored.to_dict():
            return {"status": "refused",
                    "reason": DeliveryRefusal.ANCHOR_REFUSED}
        # (ii) anchor validation INSIDE the fresh source-bound setup
        targets = {"projection": projection.to_dict(), "token": token}
        capability, context, refusal = self._run_route(targets)
        if refusal is not None:
            return {"status": "refused",
                    "reason": DeliveryRefusal.ANCHOR_REFUSED}
        try:
            anchor = self._anchor_read(outbox_recovery)
            self._store._read_commit()
        except Exception:
            self._store._read_abort()
            raise
        if anchor is None:
            return {"status": "refused",
                    "reason": DeliveryRefusal.ANCHOR_REFUSED}
        # (iii) POST-SETUP: the anchor's recovery_id EQUALS the
        # already-checked value (ONE equality chain, every named pair)
        if anchor.recovery_id != outbox_recovery:
            return {"status": "refused",
                    "reason": DeliveryRefusal.WIRE_RECOVERY_MISMATCH}
        if self._fault_no_ack:
            return {"status": "uncertain",
                    "reason": DeliveryUncertain.NO_ACK}
        # the RECEIVER'S delivery commit (DISTINCT from the issuer's
        # delivered outbox update - separate transactions)
        try:
            result = self._store.insert_evidence(
                self.owner_uid, self.lifetime_id, self.operation_id,
                projection, token, capability, context)
        except (cap.AuthError, cap.CapRefused, DeliveryDriftError,
                UnknownRecoveryError, EvidenceScopeError):
            return {"status": "refused",
                    "reason": DeliveryRefusal.ANCHOR_REFUSED}
        except sqlite3.Error:
            return {"status": "refused",
                    "reason": DeliveryRefusal.IO_FAILURE}
        # the EvidenceRef is DERIVED from the durable rows, NEVER carried
        ev = self._store._q.q(
            "SELECT evidence_id, origin_kind FROM evidence_entries"
            " WHERE recovery_id=%s AND delivery_token=%s",
            (projection.recovery_id, token), "one")
        if ev is None:
            return {"status": "uncertain",
                    "reason": DeliveryUncertain.IO_UNREADABLE}
        if result.kind in ("inserted", "replayed"):
            evidence_ref = EvidenceRef(kind="late",
                                       event_id=result.event_id,
                                       evidence_id=ev["evidence_id"])
        else:
            evidence_ref = EvidenceRef(kind="blocked",
                                       evidence_id=result.evidence_id)
        ack = AckInput(kind="ack", token=token, projection=projection,
                       committed_result=result, evidence_ref=evidence_ref)
        return {"status": "delivered", "ack": ack,
                "committed_result": result, "evidence_ref": evidence_ref}

    def verify_ack(self, input: AckInput) -> ConfirmResult:
        """verify_ack: the ACTUAL association - identity fields + the
        branch class proof; the ONE FULL source-bound route."""
        if type(input) is not AckInput:
            raise cap.ModelInputError()
        if input.kind == "refused":
            # a refused ack carries no result: verify the token/projection
            # association ONLY (a refused AckInput is NEVER 'delivered')
            targets = {"kind": "refused", "token": input.token}
            _, _, refusal = self._run_route(targets)
            if refusal is not None:
                return ConfirmResult(
                    kind="refused",
                    reason=AckRefusedReason.RECEIVER_UNAVAILABLE)
            row = self._store._q.q(
                "SELECT evidence_id FROM evidence_entries WHERE"
                " delivery_token=%s", (input.token,), "one")
            self._store._read_commit()
            if row is None:
                return ConfirmResult(
                    kind="refused", reason=AckRefusedReason.MISMATCHED_TOKEN)
            return ConfirmResult(kind="verified")
        return self._confirm(input.token, input.projection.recovery_id,
                             input.projection, input.committed_result,
                             input.evidence_ref)

    def confirm_committed_result(self, input: dict) -> ConfirmResult:
        """confirm_committed_result: the FULL input - NO weak
        token/result-only route; the SAME ONE full source-bound route."""
        if type(input) is not dict or set(input) != {
                "token", "recovery_id", "projection", "committed_result",
                "evidence_ref"}:
            raise cap.ModelInputError()
        if type(input["projection"]) is not SafeEvidenceProjection \
                or type(input["committed_result"]) is not InsertionEventResult \
                or type(input["evidence_ref"]) is not EvidenceRef:
            raise cap.ModelInputError()
        _req_hex(input["token"], 32)
        _req_str(input["recovery_id"], BOUNDED_STR_MAX)
        return self._confirm(input["token"], input["recovery_id"],
                             input["projection"], input["committed_result"],
                             input["evidence_ref"])

    def _confirm(self, token, recovery_id, projection, committed_result,
                 evidence_ref) -> ConfirmResult:
        """The ONE FULL SOURCE-BOUND route: fresh registered setup bound
        to the FULL input -> handle checks + PhaseA -> the DURABLE REREAD
        via the exact evidence/event/ledger joins; CANONICAL per-kind
        field comparison - NEVER from presented values alone (a forged
        ID/count FAILS the join)."""
        targets = {"evidence_id": evidence_ref.evidence_id,
                   "event_id": evidence_ref.event_id,
                   "kind": committed_result.kind,
                   "projection": projection.to_dict(),
                   "recovery_id": recovery_id, "token": token}
        _, _, refusal = self._run_route(targets)
        if refusal is not None:
            return ConfirmResult(kind="refused",
                                 reason=AckRefusedReason.RECEIVER_UNAVAILABLE)
        q = self._store._q
        try:
            if self._fault_io_unreadable:
                raise sqlite3.OperationalError("injected")
            row = q.q("SELECT * FROM evidence_entries WHERE"
                      " recovery_id=%s AND delivery_token=%s",
                      (recovery_id, token), "one")
            if row is None:
                self._store._read_commit()
                return ConfirmResult(
                    kind="refused", reason=AckRefusedReason.MISMATCHED_TOKEN)
            stored = SafeEvidenceProjection.from_dict(
                json.loads(row["projection_json"]))
            if stored.to_dict() != projection.to_dict():
                self._store._read_commit()
                return ConfirmResult(
                    kind="refused",
                    reason=AckRefusedReason.MISMATCHED_PROJECTION)
            expected = self._expected_result(row, committed_result.kind)
            evidence_ref_expected = self._canonical_ref(row)
            self._store._read_commit()
        except sqlite3.Error:
            try:
                self._store._read_abort()
            except Exception:
                pass
            return ConfirmResult(kind="uncertain",
                                 uncertain=ConfirmUncertain.IO_UNREADABLE)
        except Exception:
            self._store._read_abort()
            raise
        # the CANONICAL comparison: a forged ID/count FAILS the join
        if expected is None or expected != committed_result \
                or evidence_ref_expected != evidence_ref:
            return ConfirmResult(
                kind="refused", reason=AckRefusedReason.MISMATCHED_RESULT)
        return ConfirmResult(kind="verified")

    def _expected_result(self, row, kind) -> Optional[InsertionEventResult]:
        """The CANONICAL per-kind stable fields for the PRESENTED kind,
        READ FROM THE DURABLE ROWS via the exact evidence/event/ledger
        joins (NEVER from presented values; a forged ID/count FAILS the
        join -> None)."""
        if row["origin_kind"] == "blocked_arrival":
            if kind == "blocked_inserted":
                # original-1 = 1; resolution_event_id NULL ALWAYS
                # (derived from the IMMUTABLE blocked origin, even
                # after closure - the CURRENT closing proof is checked
                # SEPARATELY, NEVER written into this result field)
                return InsertionEventResult(
                    kind="blocked_inserted", contribution=1,
                    evidence_id=row["evidence_id"])
            if kind == "blocked_replay_no_event":
                # contribution 0 / stored 1; the stamp == the SELECTED
                # CURRENT evidence stamp (NULL unresolved / the EXACT
                # closing event once resolved)
                return InsertionEventResult(
                    kind="blocked_replay_no_event", contribution=0,
                    evidence_id=row["evidence_id"], stored_new_retained=1,
                    resolution_event_id=row["resolution_event_id"])
            return None
        event = self._store._q.q(
            "SELECT new_retained, inserting_uid, inserting_lifetime_id,"
            " inserting_operation_id FROM insertion_events WHERE"
            " event_id=%s", (row["insertion_event_id"],), "one")
        if event is None:
            return None  # a forged event_id FAILS the join
        # the late event/evidence identity-triple EQUALITY
        if event["inserting_uid"] != row["inserting_uid"] \
                or event["inserting_lifetime_id"] != row["inserting_lifetime_id"] \
                or event["inserting_operation_id"] != row["inserting_operation_id"]:
            return None
        if kind == "inserted":
            return InsertionEventResult(
                kind="inserted", contribution=3,
                event_id=row["insertion_event_id"])
        if kind == "replayed":
            return InsertionEventResult(
                kind="replayed", contribution=0,
                event_id=row["insertion_event_id"],
                stored_new_retained=event["new_retained"])
        return None

    def _canonical_ref(self, row) -> EvidenceRef:
        """The EvidenceRef DERIVED from the durable rows (late: event_id
        + evidence_id; blocked: evidence_id ONLY, NO event field)."""
        if row["origin_kind"] == "blocked_arrival":
            return EvidenceRef(kind="blocked",
                               evidence_id=row["evidence_id"])
        return EvidenceRef(kind="late",
                           event_id=row["insertion_event_id"],
                           evidence_id=row["evidence_id"])


# ---------------------------------------------------------------------------
# Evidence model (sec 5/6 - CLOSED SafeEvidenceProjection + EvidenceTarget)
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ScopedInventoryRef:
    owner: Lifetime
    class_name: CopyClass
    entry_id: str

    def __post_init__(self) -> None:
        if type(self.owner) is not Lifetime:
            raise cap.ModelInputError()
        _req_enum(self.class_name, CopyClass)
        _req_str(self.entry_id, BOUNDED_STR_MAX)

    def to_dict(self) -> dict:
        return {"class_name": self.class_name.value,
                "entry_id": self.entry_id,
                "kind": "ScopedInventoryRef",
                "owner": {"lifetime_id": self.owner.lifetime_id,
                          "uid": self.owner.uid}}


@dataclass(frozen=True)
class RecoveryBoundRef:
    recovery_id: str
    source: EvidenceKind

    def __post_init__(self) -> None:
        _req_str(self.recovery_id, BOUNDED_STR_MAX)
        _req_enum(self.source, EvidenceKind)

    def to_dict(self) -> dict:
        return {"kind": "RecoveryBoundRef",
                "recovery_id": self.recovery_id,
                "source": self.source.value}


def _target_dict(target) -> dict:
    if type(target) is ScopedInventoryRef:
        return target.to_dict()
    if type(target) is RecoveryBoundRef:
        return target.to_dict()
    raise cap.ModelInputError()


def _target_from_dict(obj):
    if type(obj) is not dict:
        raise cap.ModelInputError()
    try:
        if obj.get("kind") == "ScopedInventoryRef":
            return ScopedInventoryRef(
                owner=Lifetime(uid=obj["owner"]["uid"],
                               lifetime_id=obj["owner"]["lifetime_id"]),
                class_name=CopyClass(obj["class_name"]),
                entry_id=obj["entry_id"])
        if obj.get("kind") == "RecoveryBoundRef":
            return RecoveryBoundRef(recovery_id=obj["recovery_id"],
                                    source=EvidenceKind(obj["source"]))
    except (KeyError, ValueError, TypeError):
        raise cap.ModelInputError()
    raise cap.ModelInputError()


class EvidenceIntegrityError(Exception):
    """An INCOHERENT durable join (a stored evidence row whose
    insertion event is missing, or vice versa) = typed integrity
    REFUSAL - never a defaulted count (v80 FORGED FIELDS)."""


def _evidence_target_source_ok(queryable, projection, anchor_uid: int,
                               anchor_lid: int,
                               anchor_op_id: str) -> bool:
    """STORE-BUILT EXACT EQUALITY (v80 795-812, the BINDING - not mere
    validation): the projection's SAFE target/source must EQUAL the
    ORIGINAL VERIFIED scoped evidence-source association of the
    SUBJECT, built by the STORE per-target/branch. The EXPECTED SOURCE
    ASSOCIATION: the evidence kind matches the source (v80 1289). An
    UNRELATED same-owner inventory target is NOT the subject's
    evidence and does NOT satisfy the binding (REFUSAL, not
    acceptance)."""
    if projection.source != projection.kind:
        return False  # the expected source association of the anchor
    t = projection.target
    if type(t) is RecoveryBoundRef:
        # validated against the recovery anchor of the SAME subject
        return t.recovery_id == projection.recovery_id \
            and t.source == projection.kind
    if type(t) is ScopedInventoryRef:
        # validated against the SCOPED INVENTORY with EXISTENCE proof
        # (v80 795-812): the subject's OWN receipt entry ONLY - an
        # unrelated same-owner entry refuses, and a GUESSED/
        # FABRICATED receipt string with no durable receipt row is NOT
        # the subject's evidence (REFUSAL, not acceptance)
        if t.owner.uid != anchor_uid \
                or t.owner.lifetime_id != anchor_lid \
                or t.class_name is not CopyClass.OP_RECORDS \
                or t.entry_id != "receipt:" + anchor_op_id:
            return False
        row = queryable.q("SELECT 1 AS x FROM operation_receipts WHERE"
                          " uid=%s AND lifetime_id=%s AND op_id=%s AND"
                          " ref=%s",
                          (anchor_uid, anchor_lid, anchor_op_id,
                           t.entry_id), "one")
        return row is not None
    return False


def _verify_insertion_event_coupling(q, event) -> None:
    """LEDGER VALIDATION (v80 1254-1308: actual, ONE-TO-ONE, PER-CLASS),
    RE-VERIFIED at replay/readback: the event's ledger rows (count ==
    inserted_count) EACH join: class 'evidence_entries' -> the evidence
    row EXISTS with insertion_event_id == the event (event equality
    lives on the EVIDENCE row, never on the anchor) AND subject
    (subject_evidence_ref == 'evidence:' + entry_id) -> its recovery_id
    -> the anchor (resolved_accounting OR blocked_records) whose uid /
    lifetime_id / operation_id / hash (commitment|binding_hash) EQUAL
    the event's anchor identity, PLUS the expected source association
    (the evidence kind == the event source). A dangling row, wrong
    class, wrong owner/triple, wrong event, wrong anchor, wrong source,
    a missing destination, or a count mismatch = integrity REFUSAL -
    NEVER an incoherent default success."""
    rows = q.q("SELECT class_name, entry_id FROM"
               " resolution_event_ledger WHERE resolution_event_id=%s",
               (event["event_id"],), "all")
    if len(rows) != event["inserted_count"]:
        raise EvidenceIntegrityError()  # the count, from the ledger ALONE
    for row in rows:
        if row["class_name"] != "evidence_entries":
            raise EvidenceIntegrityError()  # wrong class
        if event["subject_evidence_ref"] != "evidence:" + row["entry_id"]:
            raise EvidenceIntegrityError()  # wrong subject
        ev = q.q("SELECT recovery_id, kind, insertion_event_id FROM"
                 " evidence_entries WHERE evidence_id=%s",
                 (row["entry_id"],), "one")
        if ev is None:
            raise EvidenceIntegrityError()  # a missing destination row
        if ev["insertion_event_id"] != event["event_id"]:
            raise EvidenceIntegrityError()  # wrong event
        if ev["recovery_id"] != event["recovery_id"]:
            raise EvidenceIntegrityError()
        anchor = q.q("SELECT uid, lifetime_id, operation_id, commitment"
                     " AS h FROM resolved_accounting WHERE recovery_id=%s",
                     (event["recovery_id"],), "one")
        if anchor is None:
            anchor = q.q("SELECT uid, lifetime_id, operation_id,"
                         " binding_hash AS h FROM blocked_records WHERE"
                         " recovery_id=%s", (event["recovery_id"],), "one")
        if anchor is None                 or anchor["uid"] != event["uid"]                 or anchor["lifetime_id"] != event["lifetime_id"]                 or anchor["operation_id"] != event["operation_id"]                 or anchor["h"] != event["commitment"]:
            raise EvidenceIntegrityError()  # wrong anchor/triple/owner
        if ev["kind"] != event["source"]:
            raise EvidenceIntegrityError()  # the expected source assoc


@dataclass(frozen=True)
class SafeEvidenceProjection:
    """The DURABLE CLOSED comparison structure (sec 5)."""
    recovery_id: str
    uid: int
    lifetime_id: int
    operation_id: str
    commitment: str
    kind: EvidenceKind
    target: object                # ScopedInventoryRef | RecoveryBoundRef
    source: EvidenceKind
    accounting_scope: Lifetime

    def __post_init__(self) -> None:
        _req_str(self.recovery_id, BOUNDED_STR_MAX)
        _req_int(self.uid, 0)
        _req_int(self.lifetime_id, 1)
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_hex(self.commitment, HASH_LEN)
        _req_enum(self.kind, EvidenceKind)
        _target_dict(self.target)
        _req_enum(self.source, EvidenceKind)
        if type(self.accounting_scope) is not Lifetime:
            raise cap.ModelInputError()

    def to_dict(self) -> dict:
        return {
            "accounting_scope": {
                "lifetime_id": self.accounting_scope.lifetime_id,
                "uid": self.accounting_scope.uid},
            "commitment": self.commitment,
            "kind": self.kind.value,
            "lifetime_id": self.lifetime_id,
            "operation_id": self.operation_id,
            "recovery_id": self.recovery_id,
            "source": self.source.value,
            "target": _target_dict(self.target),
            "uid": self.uid,
        }

    @staticmethod
    def from_dict(obj) -> "SafeEvidenceProjection":
        if type(obj) is not dict:
            raise cap.ModelInputError()
        try:
            return SafeEvidenceProjection(
                recovery_id=obj["recovery_id"], uid=obj["uid"],
                lifetime_id=obj["lifetime_id"],
                operation_id=obj["operation_id"],
                commitment=obj["commitment"], kind=EvidenceKind(obj["kind"]),
                target=_target_from_dict(obj["target"]),
                source=EvidenceKind(obj["source"]),
                accounting_scope=Lifetime(
                    uid=obj["accounting_scope"]["uid"],
                    lifetime_id=obj["accounting_scope"]["lifetime_id"]))
        except (KeyError, ValueError, TypeError):
            raise cap.ModelInputError()


# ---------------------------------------------------------------------------
# Receiver anchor model (sec 5): the VALIDATED anchor + the CLOSED sums
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class AnchorRow:
    uid: int
    lifetime_id: int
    operation_id: str

    def __post_init__(self) -> None:
        _req_int(self.uid, 0)
        _req_int(self.lifetime_id, 1)
        _req_hex(self.operation_id, OP_ID_LEN)


@dataclass(frozen=True)
class ResolvedSource:
    """The TYPED resolved proof (decoded resolved_accounting.source_json)."""
    anchor: str
    commitment: str
    operation_id: str
    resolver: str

    def __post_init__(self) -> None:
        _req_str(self.anchor, BOUNDED_STR_MAX)
        _req_hex(self.commitment, HASH_LEN)
        _req_hex(self.operation_id, OP_ID_LEN)
        _req_str(self.resolver, BOUNDED_STR_MAX)


@dataclass(frozen=True)
class AnchorSource:
    """CLOSED: the exact table + row identity + typed resolved proof.
    ANCHOR COHERENCE (always): table='blocked_records' IFF resolved
    NULL; 'resolved_accounting' IFF resolved present."""
    table: str
    row_key: str
    resolved: Optional[ResolvedSource] = None

    def __post_init__(self) -> None:
        _req_str(self.row_key, BOUNDED_STR_MAX)
        if self.table == "blocked_records":
            if self.resolved is not None:
                raise cap.ModelInputError()
        elif self.table == "resolved_accounting":
            if type(self.resolved) is not ResolvedSource:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class VerifiedAnchor:
    """The VALIDATED CURRENT anchor (re-read FRESH at verification time;
    typed-field equality, NO carried digest, NO cached snapshot)."""
    recovery_id: str
    anchor_kind: str            # BlockedAnchor|ResolvedAnchor
    anchor_row: AnchorRow
    commitment: str
    source: AnchorSource
    validated_at: int

    def __post_init__(self) -> None:
        _req_str(self.recovery_id, BOUNDED_STR_MAX)
        if type(self.anchor_row) is not AnchorRow:
            raise cap.ModelInputError()
        _req_hex(self.commitment, HASH_LEN)
        if type(self.source) is not AnchorSource:
            raise cap.ModelInputError()
        _req_int(self.validated_at, 0)
        # the tag/table/resolved triple is ALWAYS coherent
        if self.anchor_kind == "BlockedAnchor":
            if self.source.table != "blocked_records" \
                    or self.source.resolved is not None:
                raise cap.ModelInputError()
        elif self.anchor_kind == "ResolvedAnchor":
            if self.source.table != "resolved_accounting" \
                    or type(self.source.resolved) is not ResolvedSource:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()
        # SELECTED-ROW EQUALITY: the source row key IS the recovery_id
        if self.source.row_key != self.recovery_id:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class ConfirmResult:
    """The ONE CLOSED shape shared by verify_ack +
    confirm_committed_result: verified | refused | uncertain (DISJOINT)."""
    kind: str                     # verified|refused|uncertain
    reason: Optional[AckRefusedReason] = None
    uncertain: Optional[ConfirmUncertain] = None

    def __post_init__(self) -> None:
        if self.kind == "verified":
            if self.reason is not None or self.uncertain is not None:
                raise cap.ModelInputError()
        elif self.kind == "refused":
            _req_enum(self.reason, AckRefusedReason)
            if self.uncertain is not None:
                raise cap.ModelInputError()
        elif self.kind == "uncertain":
            _req_enum(self.uncertain, ConfirmUncertain)
            if self.reason is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


# ---------------------------------------------------------------------------
# IssuerHarness (sec 5 - TYPED, PRIVATE/test-only, INJECTED standalone
# issuer-store boundary; the receiver path does NOT own it; NO public
# surface expansion). Its fixture_store_conn is a SEPARATE connection.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ComposeResult:
    token: str                    # 32-hex, minted BEFORE send, persisted
    recovery_id: str
    projection: SafeEvidenceProjection

    def __post_init__(self) -> None:
        _req_hex(self.token, 32)
        _req_str(self.recovery_id, BOUNDED_STR_MAX)
        if type(self.projection) is not SafeEvidenceProjection:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class PersistConfirmation:
    token: str
    persisted: bool
    fsynced_at: int

    def __post_init__(self) -> None:
        _req_hex(self.token, 32)
        if self.persisted is not True:
            raise cap.ModelInputError()
        _req_int(self.fsynced_at, 0)


class AnchorRefusal(Enum):
    """The CLOSED anchor-refusal reasons (sec 5 provider contract)."""
    UNKNOWN_RECOVERY = "unknown_recovery"
    STALE_ANCHOR = "stale_anchor"
    HANDLE_INVALID = "handle_invalid"
    PHASEA_FAILED = "phasea_failed"
    PROJECTION_MISMATCH = "projection_mismatch"


class DeliveryRefusal(Enum):
    """The CLOSED delivery-refusal reasons; wire_recovery_mismatch has
    the EXACT named predicate (the recovery_id equality chain) and NEVER
    launders an IO failure."""
    UNCONFIRMED_COMMIT = "unconfirmed_commit"
    ANCHOR_REFUSED = "anchor_refused"
    IO_FAILURE = "io_failure"
    WIRE_RECOVERY_MISMATCH = "wire_recovery_mismatch"


class DeliveryUncertain(Enum):
    NO_ACK = "no_ack"
    IO_UNREADABLE = "io_unreadable"


class ConfirmUncertain(Enum):
    """'uncertain' ONLY when the confirm's DURABLE REREAD is unreadable;
    NO AckRefusedReason maps to 'uncertain'."""
    IO_UNREADABLE = "io_unreadable"


class AckRefusedReason(Enum):
    MISMATCHED_TOKEN = "mismatched_token"
    MISMATCHED_RECOVERY = "mismatched_recovery"
    MISMATCHED_PROJECTION = "mismatched_projection"
    MISMATCHED_RESULT = "mismatched_result"
    RECEIVER_UNAVAILABLE = "receiver_unavailable"
    IO_FAILURE = "io_failure"


class AckUncertain(Enum):
    LOST_COMMIT_CONFIRMATION = "lost_commit_confirmation"
    IO_UNREADABLE = "io_unreadable"


@dataclass(frozen=True)
class EvidenceRef:
    """The receiver's COMMITTED SUM result identity."""
    kind: str                     # late|blocked
    event_id: Optional[str] = None
    evidence_id: Optional[str] = None

    def __post_init__(self) -> None:
        if self.kind == "late":
            # the MINIMAL CLOSED sum: event_id + evidence_id
            _req_str(self.event_id, BOUNDED_STR_MAX)
            _req_hex(self.evidence_id, 32)
        elif self.kind == "blocked":
            # blocked: evidence_id ONLY - NO event field AT ALL
            _req_hex(self.evidence_id, 32)
            if self.event_id is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class AckInput:
    """ONE CLOSED TAGGED UNION: ack | refused. An 'ack' carries the FULL
    VALIDATED payload: projection + committed_result + evidence_ref."""
    kind: str                     # ack|refused
    token: str
    projection: Optional[SafeEvidenceProjection] = None
    committed_result: Optional["InsertionEventResult"] = None
    evidence_ref: Optional[EvidenceRef] = None
    reason: Optional[AckRefusedReason] = None

    def __post_init__(self) -> None:
        _req_hex(self.token, 32)
        if self.kind == "ack":
            if type(self.projection) is not SafeEvidenceProjection \
                    or type(self.committed_result) is not InsertionEventResult \
                    or type(self.evidence_ref) is not EvidenceRef \
                    or self.reason is not None:
                raise cap.ModelInputError()
        elif self.kind == "refused":
            _req_enum(self.reason, AckRefusedReason)
            if self.projection is not None or self.committed_result is not None \
                    or self.evidence_ref is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class AckResult:
    """CLOSED: delivered | still_pending | uncertain."""
    kind: str
    token: str
    reason: Optional[AckRefusedReason] = None
    uncertain: Optional[AckUncertain] = None

    def __post_init__(self) -> None:
        _req_hex(self.token, 32)
        if self.kind == "delivered":
            if self.reason is not None or self.uncertain is not None:
                raise cap.ModelInputError()
        elif self.kind == "still_pending":
            _req_enum(self.reason, AckRefusedReason)
            if self.uncertain is not None:
                raise cap.ModelInputError()
        elif self.kind == "uncertain":
            _req_enum(self.uncertain, AckUncertain)
            if self.reason is not None:
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()


@dataclass(frozen=True)
class InsertionEventResult:
    """The receiver's COMMITTED insertion SUM (sec 5 batch accounting)."""
    kind: str                     # inserted|blocked_inserted|replayed|blocked_replay_no_event
    contribution: int
    event_id: Optional[str] = None
    evidence_id: Optional[str] = None
    stored_new_retained: Optional[int] = None
    resolution_event_id: Optional[str] = None  # NULL pre-closure; the
    # closing stamp after resolution (blocked-origin replay only)

    def __post_init__(self) -> None:
        if self.kind == "inserted":
            _req_int(self.contribution, 3, 3)
            _req_str(self.event_id, BOUNDED_STR_MAX)
        elif self.kind == "blocked_inserted":
            _req_int(self.contribution, 1, 1)
            _req_hex(self.evidence_id, 32)
            if self.event_id is not None:  # the field EXISTS and is NULL
                raise cap.ModelInputError()
        elif self.kind == "replayed":
            _req_int(self.contribution, 0, 0)
            _req_str(self.event_id, BOUNDED_STR_MAX)
            _req_int(self.stored_new_retained, 0)
        elif self.kind == "blocked_replay_no_event":
            _req_int(self.contribution, 0, 0)
            _req_hex(self.evidence_id, 32)
            _req_int(self.stored_new_retained, 0)
            if self.event_id is not None:  # the field EXISTS and is NULL
                raise cap.ModelInputError()
        else:
            raise cap.ModelInputError()
        if self.kind in ("inserted", "replayed") \
                and self.evidence_id is not None:
            raise cap.ModelInputError()


_OUTBOX_DDL = """
CREATE TABLE IF NOT EXISTS outbox(
  token TEXT PRIMARY KEY, recovery_id TEXT NOT NULL,
  projection_json TEXT NOT NULL, state TEXT NOT NULL,
  composed_at INTEGER NOT NULL, delivered_at INTEGER);
CREATE TABLE IF NOT EXISTS scrub_log(
  token TEXT PRIMARY KEY, scrubbed_at INTEGER NOT NULL);
"""


class IssuerHarness:
    """PRIVATE/test-only typed issuer-store boundary (sec 5). Owns a
    SEPARATE disposable connection (fixture_store_conn); NO cross-DB
    atomicity with the receiver is claimed."""

    def __init__(self, fixture_store_conn: sqlite3.Connection,
                 clock: StoreClock, snapshots: FactStoreSnapshotStore,
                 receiver_context: ReceiverContextProvider) -> None:
        """Deps are CONCRETE and EXACTLY: fixture_store_conn (the
        fixture's OWN file connection backing the outbox - SEPARATE from
        the FactStore conn), clock (SECONDS, shared with the fixtures),
        snapshots, AND receiver_context (the INJECTED
        ReceiverContextProvider - a DECLARED dep)."""
        if type(fixture_store_conn) is not sqlite3.Connection:
            raise cap.ModelInputError()
        if type(snapshots) is not FactStoreSnapshotStore:
            raise cap.ModelInputError()
        if type(receiver_context) is not ReceiverContextProvider:
            raise cap.ModelInputError()
        self._conn = fixture_store_conn
        self._conn.isolation_level = None
        self._conn.executescript(_OUTBOX_DDL)
        self._clock = clock
        self._snapshots = snapshots
        self._receiver = receiver_context
        self._pending_hook = None

    def _verify_stage(self, stage: str, token: str):
        """FIVE-STAGE EQUALITY (sec 5), RE-VERIFIED at EVERY stage
        (compose, persist, send, restart, AND the receiver+ack checks):
        the STORED projection/token/pending state is re-read and
        outbox.recovery_id == ref_json.recovery_id == the VALIDATED
        recovery anchor; a mismatch at ANY stage = REFUSAL (no mint, no
        send, no ack, no count, no state change)."""
        row = self._conn.execute(
            "SELECT recovery_id, projection_json, state FROM outbox"
            " WHERE token=?", (token,)).fetchone()
        if row is None:
            raise HarnessRefusedError(stage, "unknown_token")
        stored = SafeEvidenceProjection.from_dict(json.loads(row[1]))
        if stored.recovery_id != row[0]:
            raise HarnessRefusedError(stage, "recovery_mismatch")
        verdict = self._receiver.verify_anchor(
            {"recovery_id": row[0], "projection": stored})
        if verdict["status"] != "verified":
            raise HarnessRefusedError(
                stage, "anchor_refused:" + verdict["reason"].value)
        if verdict["anchor"].recovery_id != row[0]:
            raise HarnessRefusedError(stage, "anchor_mismatch")
        return row, stored

    def compose(self, delivery: dict) -> ComposeResult:
        """Mint the delivery token BEFORE send (sec 5 delivery identity)."""
        if type(delivery) is not dict:
            raise cap.ModelInputError()
        projection = delivery.get("projection")
        recovery_id = delivery.get("recovery_id")
        if type(projection) is not SafeEvidenceProjection:
            raise cap.ModelInputError()
        _req_str(recovery_id, BOUNDED_STR_MAX)
        if projection.recovery_id != recovery_id:
            raise cap.ModelInputError()
        # FIVE-STAGE (compose): the recovery anchor must verify BEFORE
        # the token mint - a mismatch = REFUSAL, no mint, no state change
        verdict = self._receiver.verify_anchor(
            {"recovery_id": recovery_id, "projection": projection})
        if verdict["status"] != "verified":
            raise HarnessRefusedError(
                "compose", "anchor_refused:" + verdict["reason"].value)
        token = secrets.token_hex(16)
        now = _clock_now(self._clock)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO outbox(token, recovery_id, projection_json,"
                " state, composed_at) VALUES(?,?,?,?,?)",
                (token, recovery_id,
                 _canon(projection.to_dict()).decode("utf-8"), "pending",
                 now))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        return ComposeResult(token=token, recovery_id=recovery_id,
                             projection=projection)

    def confirm_persisted(self, token: str) -> PersistConfirmation:
        _req_hex(token, 32)
        self._verify_stage("persist", token)  # five-stage, persist stage
        return PersistConfirmation(token=token, persisted=True,
                                   fsynced_at=_clock_now(self._clock))

    def send(self, token: str) -> dict:
        """The SEND stage: re-read + re-verify the STORED state BEFORE
        dispatch; returns the wire payload {token, recovery_id,
        projection}. A mismatch = HarnessRefusedError, NO send."""
        _req_hex(token, 32)
        row, stored = self._verify_stage("send", token)
        if row[2] != "pending":
            raise HarnessRefusedError("send", "not_pending")
        return {"projection": stored, "recovery_id": row[0],
                "token": token}

    def outbox_state(self, token: str) -> str:
        _req_hex(token, 32)
        row = self._conn.execute(
            "SELECT state FROM outbox WHERE token=?", (token,)).fetchone()
        if row is None:
            raise cap.ModelInputError()
        return row[0]

    _SCRUB_CONTAINERS = ("raw_ref_handle", "comparison_buffer")

    def scrub_hook(self, refs) -> dict:
        """The EXACT v80 HOOK SHAPE (v80 2060-2137): scrub(refs:
        OwnedContainers) -> ScrubOutcome - DISCARD FIRST, THEN the
        live ABSENCE check; the TYPED CLOSED witness
        'cleared'|'refused' with SAFE OPAQUE handle LABELS ONLY.
        NO recording, NO stage verification, NO token: the token
        wrapper (scrub) owns the completion record; this hook is
        ALSO the teardown hook (the SAME hook shape the fixture
        invokes post-known-commit AND at teardown)."""
        if not isinstance(refs, dict):
            raise cap.ModelInputError()
        if set(refs.keys()) != set(self._SCRUB_CONTAINERS):
            raise cap.ModelInputError()  # the CLOSED enumeration, exact
        # DISCARD FIRST: each volatile container is emptied before any
        # check runs (fixture-owned reference destruction). A destroy/
        # clear FAILURE is CONTAINED (typed refusal below) - NEVER a
        # raw exception escaping, NEVER an implicit pass.
        destroy_failed = False
        for name in self._SCRUB_CONTAINERS:
            c = refs[name]
            if c is None:
                continue
            destroy = getattr(c, "destroy", None)
            try:
                if callable(destroy):
                    destroy()
                elif hasattr(c, "clear"):
                    c.clear()
                else:
                    destroy_failed = True  # NOTHING can discard it
            except Exception:
                destroy_failed = True  # the raw may remain
        # THEN the live check verifies ABSENCE in BOTH containers.
        # ONLY an explicit live_raw() == False probe proves absence;
        # a missing or failed probe (or an arbitrary len/bool) is
        # UNKNOWN - and UNKNOWN never clears.
        checked = []
        raw_found = False
        unknown = destroy_failed
        for name in self._SCRUB_CONTAINERS:
            c = refs[name]
            checked.append(name)
            if c is None:
                continue
            live = getattr(c, "live_raw", None)
            if callable(live):
                try:
                    probe = live()
                except Exception:
                    unknown = True  # a FAILED probe is UNKNOWN
                else:
                    if type(probe) is not bool:
                        # a NON-BOOL probe (None/0/empty/truthy) is
                        # UNKNOWN - NEVER absence, NEVER presence
                        unknown = True
                    elif probe:
                        raw_found = True
            else:
                unknown = True  # NO probe = UNKNOWN, never a pass
        now = _clock_now(self._clock)
        # the CLOSED BOOL (v80 2083): raw_found is ALWAYS a strict
        # bool. An UNKNOWN/failed/non-bool probe is CONSERVATIVELY
        # NOT-PROVEN-EMPTY (raw_found=True forces refusal); ONLY a
        # COMPLETE set of strict literal-False probes proves absence
        # (raw_found=False) - the strict literal-False proven-empty
        # is required BEFORE 'cleared'.
        if unknown:
            raw_found = True
        live_check = {"checked_at": now, "containers": checked,
                      "raw_found": raw_found}
        complete = checked == list(self._SCRUB_CONTAINERS)
        # ENFORCEMENT: cleared IFF complete AND raw_found is STRICTLY
        # False (every probe returned the literal False and nothing
        # was unknown/failed); a 'cleared' + raw_found=true
        # CONTRADICTION, a None/0/empty/non-bool probe, an unknown/
        # failed probe, an incomplete check, or a failed destroy =
        # 'refused' with safe IDs (v80 2060-2137).
        kind = "cleared" if (complete and raw_found is False
                             ) else "refused"
        outcome = {"kind": kind,
                   "ref_ids": list(self._SCRUB_CONTAINERS),
                   "live_check": live_check}
        return outcome

    def scrub(self, token: str, refs) -> dict:
        """VOLATILE ERASURE (v80 2060-2137, EXACT): scrub(refs:
        OwnedContainers) -> ScrubOutcome. OwnedContainers = the CLOSED
        enumeration {raw_ref_handle, comparison_buffer} - the ONLY
        volatile containers holding erased detail. ScrubOutcome =
        {kind: 'cleared'|'refused', ref_ids: SAFE OPAQUE handle LABELS
        ONLY (NEVER raw/live reference objects), live_check:
        {checked_at, containers, raw_found}}. ORDER: the raw refs are
        DISCARDED FIRST, THEN the live check verifies ABSENCE;
        'cleared' is emitted IFF the check is COMPLETE (BOTH
        containers), PASSING, and raw_found=false - BEFORE the outcome
        is emitted and BEFORE completion is recorded. A 'cleared'
        outcome with raw_found=true is a CONTRADICTION = REFUSAL; an
        unknown/incomplete/failed check = 'refused' with safe IDs.
        raw_found is the CLOSED bool: True when raw was found OR
        absence is UNPROVEN (a None/0/empty/non-bool/failed/missing
        probe = conservative True); False ONLY on a COMPLETE strict
        literal-False probe set. NOTHING is recorded and NO state
        changes on 'refused'."""
        _req_hex(token, 32)
        outcome = self.scrub_hook(refs)
        if outcome["kind"] != "cleared":
            return outcome  # REFUSAL: nothing recorded, no state change
        self._verify_stage("scrub", token)  # re-verified AFTER the raw
        now = _clock_now(self._clock)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "INSERT INTO scrub_log(token, scrubbed_at) VALUES(?,?)",
                (token, now))
            self._conn.execute("COMMIT")
        except Exception:
            self._conn.execute("ROLLBACK")
            raise
        # the CLOSED result schema is EXACT: kind/ref_ids/live_check
        # ONLY (the scrub_log row carries the recording - the outcome
        # carries NO extra fields)
        return outcome

    def redeliver_pending(self) -> dict:
        """PENDING RESTART: unacked rows redelivered with the SAME token;
        the RESTART stage re-verifies every row BEFORE redispatch - a
        mismatch is REFUSED (NOT redelivered, no state change)."""
        rows = self._conn.execute(
            "SELECT token FROM outbox WHERE state='pending'"
            " ORDER BY composed_at").fetchall()
        redelivered = []
        for r in rows:
            try:
                self._verify_stage("restart", r[0])
            except HarnessRefusedError:
                continue
            redelivered.append(r[0])
        return {"redelivered": redelivered}

    def _ack_durable_reread(self, token: str,
                            ack_input=None) -> AckResult:
        """LOST-CONFIRMATION REREAD (v80 1628-1645, V34b): under the
        SAME token/recovery/projection/source identity - a POSITIVE
        same-identity PENDING row = re-attempt (the row keeps the
        SAME token; redelivery replays receiver-side); DELIVERED =
        done; a MISSING, UNREADABLE, UNKNOWN-STATE, or INCOHERENT
        (identity-mismatched) row = typed 'uncertain'. An UNDECODABLE/
        shape-incoherent durable row and a MISSING binding input (no
        validated projection) are 'uncertain' too - NEVER a raw
        exception, NEVER an unvalidated success. NEVER a blind pending
        claim, NEVER blind redelivery."""
        try:
            row = self._conn.execute(
                "SELECT recovery_id, projection_json, state FROM"
                " outbox WHERE token=?", (token,)).fetchone()
        except Exception:
            return AckResult(kind="uncertain", token=token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        if row is None:
            # MISSING: the update's fate cannot be established - a
            # blind pending claim would risk a double effect
            return AckResult(kind="uncertain", token=token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        # SAME-IDENTITY coherence: the durable row must carry the SAME
        # recovery + projection (source included) as the ack'd
        # delivery; an incoherent row is uncertain, never pending
        projection = getattr(ack_input, "projection", None)
        if type(projection) is not SafeEvidenceProjection:
            # NO validated same-identity binding input (ack_input None
            # or a missing projection): the row's identity CANNOT be
            # established - typed uncertain, NEVER an unvalidated
            # delivered/pending claim off the raw row state
            return AckResult(kind="uncertain", token=token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        if row[0] != projection.recovery_id:
            return AckResult(kind="uncertain", token=token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        try:
            stored = SafeEvidenceProjection.from_dict(
                json.loads(row[1]))
        except Exception:
            # an UNDECODABLE or shape-incoherent durable row is
            # UNREADABLE - a typed uncertain, NEVER a raw exception
            # escaping the reread
            return AckResult(kind="uncertain", token=token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        if stored.to_dict() != projection.to_dict():
            return AckResult(kind="uncertain", token=token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        if row[2] == "delivered":
            return AckResult(kind="delivered", token=token)
        if row[2] == "pending":
            return AckResult(kind="still_pending", token=token,
                             reason=AckRefusedReason.IO_FAILURE)
        return AckResult(kind="uncertain", token=token,
                         uncertain=AckUncertain.IO_UNREADABLE)

    def ack(self, ack_input: AckInput,
            confirm: bool = True) -> AckResult:
        """The DELIVERED UPDATE. confirm=False simulates a LOST COMMIT
        CONFIRMATION: the update MAY have landed; the durable reread
        under the SAME identity establishes actual state."""
        if type(ack_input) is not AckInput:
            raise cap.ModelInputError()
        row = self._conn.execute(
            "SELECT recovery_id, projection_json, state FROM outbox"
            " WHERE token=?", (ack_input.token,)).fetchone()
        if row is None:
            return AckResult(kind="still_pending", token=ack_input.token,
                             reason=AckRefusedReason.MISMATCHED_TOKEN)
        if ack_input.kind == "refused":
            return AckResult(kind="still_pending", token=ack_input.token,
                             reason=ack_input.reason)
        stored = SafeEvidenceProjection.from_dict(
            json.loads(row[1]))
        if ack_input.projection.recovery_id != row[0]:
            return AckResult(kind="still_pending", token=ack_input.token,
                             reason=AckRefusedReason.MISMATCHED_RECOVERY)
        if ack_input.projection.to_dict() != stored.to_dict():
            return AckResult(kind="still_pending", token=ack_input.token,
                             reason=AckRefusedReason.MISMATCHED_PROJECTION)
        # the RECEIVER+ACK stage: the fixture snapshot re-read +
        # committed_result/evidence_ref EQUALITY through the provider's
        # ONE full source-bound route (an invented EvidenceRef or a
        # forged result is REFUSED, never 'delivered')
        try:
            self._snapshots.read(self._receiver.owner_uid)
        except Exception:
            return AckResult(kind="still_pending", token=ack_input.token,
                             reason=AckRefusedReason.RECEIVER_UNAVAILABLE)
        confirm_result = self._receiver.confirm_committed_result(
            {"token": ack_input.token, "recovery_id": row[0],
             "projection": ack_input.projection,
             "committed_result": ack_input.committed_result,
             "evidence_ref": ack_input.evidence_ref})
        if confirm_result.kind == "uncertain":
            return AckResult(kind="uncertain", token=ack_input.token,
                             uncertain=AckUncertain.IO_UNREADABLE)
        if confirm_result.kind != "verified":
            return AckResult(kind="still_pending", token=ack_input.token,
                             reason=confirm_result.reason)
        if row[2] == "delivered":
            return AckResult(kind="delivered", token=ack_input.token)
        if not confirm:
            # LOST COMMIT CONFIRMATION (v80 1628-1645): the update MAY
            # have landed - the durable reread decides; NEVER an
            # immediate uncertain/pending claim.
            return self._ack_durable_reread(ack_input.token,
                                            ack_input)
        now = _clock_now(self._clock)
        self._conn.execute("BEGIN IMMEDIATE")
        try:
            self._conn.execute(
                "UPDATE outbox SET state='delivered', delivered_at=?"
                " WHERE token=?", (now, ack_input.token))
            self._conn.execute("COMMIT")
        except Exception:
            try:
                self._conn.execute("ROLLBACK")
            except Exception:
                pass  # a LANDED commit cannot be rolled back
                # (OperationalError) - the reread establishes state
            return self._ack_durable_reread(ack_input.token,
                                            ack_input)
        return AckResult(kind="delivered", token=ack_input.token)


# ---------------------------------------------------------------------------
# FactStore (sec 13) - the seam core
# ---------------------------------------------------------------------------

def _issuer_canon(issuer) -> Optional[dict]:
    """The canonical JSON form of the TYPED issuer union (sec 4)."""
    if issuer is None:
        return None
    if type(issuer) is env.SessionRef:
        return {"kind": "session",
                "session_token_hash": issuer.session_token_hash}
    if type(issuer) is env.TelegramIssuer:
        return {"kind": "telegram", "bot_id": issuer.bot_id,
                "chat_id": issuer.chat_id, "update_id": issuer.update_id}
    raise cap.ModelInputError()


def _typed_issuer_from_json(issuer_json: str):
    """issuer_state.issuer_json -> the TYPED owner/issuer (sec 4)."""
    if type(issuer_json) is not str:
        raise cap.ModelInputError()
    try:
        d = json.loads(issuer_json)
    except ValueError:
        raise cap.ModelInputError()
    if not isinstance(d, dict) or not d:
        return None
    if d.get("kind") == "session":
        return env.SessionRef(session_token_hash=d["session_token_hash"])
    if d.get("kind") == "telegram":
        return env.TelegramIssuer(bot_id=d["bot_id"], chat_id=d["chat_id"],
                                  update_id=d["update_id"])
    raise cap.ModelInputError()


def _context_canon(context: "env.ModelOnlyCallerContext") -> dict:
    """The canonical form of the TYPED caller context (sec 13)."""
    return {
        "original_binding": (context.original_binding.projection_dict()
                             if context.original_binding is not None
                             else None),
        "current_uid": context.current_uid,
        "current_lifetime": context.current_lifetime,
        "current_capture_epoch": context.current_capture_epoch,
        "current_grant_generation": context.current_grant_generation,
        "current_policy_revision": context.current_policy_revision,
        "current_decision_revision": context.current_decision_revision,
        "current_consent_generation": context.current_consent_generation,
        "current_issuer": _issuer_canon(context.current_issuer),
        "current_issuer_valid": bool(context.current_issuer_valid)}


MODEL_TTL_SECONDS = 3600

# The EXCLUSIVE cross-instance isolation fixture (contract sec 5): the
# disposable test DB is opened with an EXCLUSIVE file lock; a second
# process/instance attempting to open it REFUSES. The lock is ISOLATION
# ONLY - it NEVER supplies liveness or death evidence about any session
# or attempt. sqlite3.Connection carries no attributes, so the fixture
# marker lives in this module-level registry keyed by id(conn).
_EXCLUSIVE_DB_LOCKS: dict = {}


def open_exclusive_db(path: str) -> sqlite3.Connection:
    """Open the disposable file-backed test DB under an EXCLUSIVE lock."""
    if type(path) is not str or not 1 <= len(path) <= 1000:
        raise cap.ModelInputError()
    lock_fd = os.open(path + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(lock_fd)
        raise cap.ModelInputError()  # a second opener REFUSES
    conn = sqlite3.connect(path)
    # a STRONG reference is held so id(conn) can NEVER be recycled
    # while registered - an unmarked raw connection reusing a freed
    # id can never inherit the marker (lifetime-reuse fence)
    _EXCLUSIVE_DB_LOCKS[id(conn)] = (conn, lock_fd)
    return conn


def close_exclusive_db(conn: sqlite3.Connection) -> None:
    """Close the fixture DB and release its exclusive lock."""
    entry = _EXCLUSIVE_DB_LOCKS.pop(id(conn), None)
    lock_fd = entry[1] if entry is not None else None
    conn.close()
    if lock_fd is not None:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)



class _EnvClockAdapter(env.ModelOnlyClock):
    """Adapts the PR-5 StoreClock to PR-4's ModelOnlyClock base class."""

    def __init__(self, inner) -> None:
        self._inner = inner

    def now(self) -> int:
        return _clock_now(self._inner)


class FactStore:
    """PR-5 FactStore seam core. MODEL ONLY - see module docstring."""

    def __init__(self, conn: sqlite3.Connection, clock: StoreClock,
                 snapshots: Optional[FactStoreSnapshotStore] = None) -> None:
        if type(conn) is not sqlite3.Connection:
            raise cap.ModelInputError()
        db_file = conn.execute("PRAGMA database_list").fetchone()
        if db_file is not None and db_file[2] \
                and id(conn) not in _EXCLUSIVE_DB_LOCKS:
            raise cap.ModelInputError()  # file-backed models REQUIRE the
            # exclusive cross-instance isolation fixture (contract sec 5)
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys=ON")
        self._q = _Queryable(conn)
        self._q.raw.executescript(_DDL)
        self._clock = clock
        _clock_now(clock)
        self._snapshots = (snapshots if snapshots is not None
                           else FactStoreSnapshotStore(self._q))
        self._evaluator = cap.ModelOnlyEvaluator(
            SyntheticProofVerifier(), self._snapshots, clock,
            MODEL_TTL_SECONDS)
        now = _clock_now(clock)
        key = env.SyntheticKey(
            algorithm=env.Algorithm.FERNET, format_version=1, key_id="k1",
            not_before=0, decrypt_until=now + 10**9,
            material=__import__('base64').urlsafe_b64encode(
                secrets.token_bytes(32)))
        self._keyring = env.SyntheticKeyRing(keys=(key,))
        self._env_clock = _EnvClockAdapter(clock)
        self._registry = env.ModelOnlyRegistryModel(
            env.SyntheticRegistryAdapter(conn), self._env_clock)
        # OWNED SESSION MINT/LOOKUP (sec 13): minted ONCE at open, the
        # single-entry live-session registry; in-memory ONLY.
        self._session_id = secrets.token_hex(16)
        self._live_sessions = frozenset({self._session_id})
        # issued-handle registry: handle -> (inner object, binding_hash)
        self._handles: dict = {}
        # producer-mint registry (sec 13): wrap() accepts ONLY objects
        # minted by the store's OWNED producer THIS SESSION - a copy or
        # unproduced object is a MISSING_IDENTITY refusal
        self._minted: dict = {}  # id(obj) -> obj (STRONG ref: an id can
        # NEVER be recycled while registered - B5 lifetime-reuse fence)
        # registered READ contexts: handle -> {caller, clock_reading}
        # captured at mint; the projection comparison RE-USES the
        # registered context (never a literal/fresh-clock rebuild)
        self._read_contexts: dict = {}
        # in-memory LOCAL uncertain associations (never durable proof)
        self._uncertain: dict = {}
        # test fault hooks (fixture-only, never production): a lost
        # commit confirmation / a failed post-commit read
        self._fault_commit_lost = False
        self._fault_readback_cause: Optional[ReadbackCause] = None
        self._producer = _StoreProducer(self)

    # -- session + handle machinery (sec 13) --------------------------------

    @property
    def session_id(self) -> str:
        return self._session_id

    def _register_handle(self, inner, binding_hash: str) -> str:
        _req_hex(binding_hash, HASH_LEN)
        handle = secrets.token_hex(16)
        self._handles[handle] = (inner, binding_hash)
        return handle

    def _validate_handle(self, bound, expected_type, label: str):
        """HANDLE VALIDATION (exact): session equality + registry presence +
        registered inner object AND binding IDENTICAL to the carried ones."""
        if type(bound) is not expected_type:
            raise cap.AuthError()  # MISSING_IDENTITY
        if bound.session_id != self._session_id:
            raise cap.AuthError()
        entry = self._handles.get(bound.handle)
        if entry is None:
            raise cap.AuthError()
        registered_inner, registered_hash = entry
        if registered_inner is not bound.inner:
            raise cap.AuthError()
        if registered_hash != bound.binding_hash:
            raise cap.AuthError()
        return bound.inner

    def _caller_context(self, binding: "env.ModelOnlyRequestBinding"
                        ) -> "env.ModelOnlyCallerContext":
        """The store's OWNED current issuer/snapshot context (sec 13)."""
        uid = binding.uid
        snap = self._snapshots.read(uid)
        issuer_row = self._q.q("SELECT issuer_json, valid FROM issuer_state"
                               " WHERE uid=%s", (uid,), "one")
        if issuer_row is None:
            # NO default-true: an unproven issuer row is INVALID (sec 4)
            current_issuer, valid = binding.issuer, False
        else:
            current_issuer = _typed_issuer_from_json(
                issuer_row["issuer_json"]) or binding.issuer
            valid = bool(issuer_row["valid"])
        return env.ModelOnlyCallerContext(
            original_binding=binding, current_uid=uid,
            current_lifetime=binding.lifetime,
            current_capture_epoch=snap.current_epoch,
            current_grant_generation=snap.grants_generation,
            current_policy_revision=snap.policy_revision,
            current_decision_revision=snap.decision_revision,
            current_consent_generation=snap.consent_generation,
            current_issuer=current_issuer, current_issuer_valid=valid)

    # -- fixture setup helpers (test-facing; producer-OWNED mints) ----------

    def setup_owner(self, uid: int, lifetime_id: int = 1,
                    epoch: int = 1,
                    scopes=(), now: Optional[int] = None) -> None:
        """Fixtures NEVER become authority: populate the OWNED fixture tables
        for a synthetic owner (grants/consents/policy/envelope/snapshot)."""
        _req_int(uid, 0)
        _req_int(lifetime_id, 1)
        _req_int(epoch, 1)
        if now is None:
            now = _clock_now(self._clock)
        q = self._q
        q.q("INSERT INTO owners(uid, lifetime_id, created_at)"
            " VALUES(%s,%s,%s)", (uid, lifetime_id, now), "exec")
        q.q("INSERT INTO owner_counters(user_id, next_epoch)"
            " VALUES(%s,%s)", (uid, epoch + 1), "exec")
        q.q("INSERT INTO marker_keys(uid, lifetime_id, fixture_key,"
            " created_at) VALUES(%s,%s,%s,%s)",
            (uid, lifetime_id, secrets.token_hex(16), now), "exec")
        q.q("INSERT INTO lifetime_state(uid, lifetime_id, generation)"
            " VALUES(%s,%s,%s)", (uid, lifetime_id, 1), "exec")
        q.q("INSERT INTO issuer_state(uid, issuer_json, valid, updated_at)"
            " VALUES(%s,%s,%s,%s)",
            (uid, _canon(_issuer_canon(env.SessionRef(
                session_token_hash=secrets.token_hex(32)))).decode("utf-8"),
             1, now), "exec")
        q.q("INSERT INTO fixture_snapshot(uid, lifetime_id, current_epoch,"
            " grants_generation, policy_revision, decision_revision,"
            " consent_generation) VALUES(%s,%s,%s,0,0,0,0)",
            (uid, lifetime_id, epoch), "exec")
        q.q("INSERT INTO fixture_policy(uid, extraction, sharing,"
            " group_write) VALUES(%s,%s,%s,%s)",
            (uid, cap.ExtractionPolicy.EXPLICIT_ONLY.value,
             cap.SharingPolicy.EXPLICIT_PER_OP.value,
             cap.GroupEligibility.NEVER.value), "exec")
        q.q("INSERT INTO fixture_envelope(uid, extraction, sharing,"
            " group_write, issued_at, expires_at, revision)"
            " VALUES(%s,%s,%s,%s,%s,%s,0)",
            (uid, cap.ExtractionPolicy.EXPLICIT_ONLY.value,
             cap.SharingPolicy.EXPLICIT_PER_OP.value, cap.GroupEligibility.NEVER.value,
             now, now + 10**9), "exec")
        for scope in scopes:
            _req_enum(scope, cap.Scope)
            q.q("INSERT INTO fixture_grants(uid, scope, granted_at,"
                " expires_at, per_op_required) VALUES(%s,%s,%s,%s,0)",
                (uid, scope.value, now, None), "exec")

    def bump_epoch(self, uid: int, new_epoch: int) -> None:
        """Fixture-only epoch movement (tests drive STALE/EPOCH vectors)."""
        _req_int(new_epoch, 1)
        self._q.q("UPDATE fixture_snapshot SET current_epoch=%s WHERE uid=%s",
                  (new_epoch, uid), "exec")

    def revoke_grants(self, uid: int) -> None:
        """Fixture-only grant revocation (tests drive REVOKED vectors)."""
        self._q.q("UPDATE fixture_snapshot SET grants_generation="
                  " grants_generation+1 WHERE uid=%s", (uid,), "exec")

    def _stored_issuer(self, uid: int):
        """The TYPED current issuer from issuer_state (None when absent)."""
        row = self._q.q("SELECT issuer_json FROM issuer_state WHERE uid=%s",
                        (uid,), "one")
        if row is None:
            return None
        return _typed_issuer_from_json(row["issuer_json"])

    def set_issuer(self, uid: int, issuer, valid: bool) -> None:
        """Fixture: set the CURRENT typed issuer + validity (sec 4)."""
        _req_int(uid, 0)
        _req_bool(valid)
        canon = _issuer_canon(issuer)
        if canon is None:
            raise cap.ModelInputError()
        self._q.q("UPDATE issuer_state SET issuer_json=%s, valid=%s,"
                  " updated_at=%s WHERE uid=%s",
                  (_canon(canon).decode("utf-8"), 1 if valid else 0,
                   _clock_now(self._clock), uid), "exec")

    def set_issuer_valid(self, uid: int, valid: bool) -> None:
        _req_bool(valid)
        self._q.q("UPDATE issuer_state SET valid=%s, updated_at=%s"
                  " WHERE uid=%s",
                  (1 if valid else 0, _clock_now(self._clock), uid), "exec")

    # -- producer-facing wrap (PRIVATE store helper; sec 13) -----------------

    def _register_minted(self, obj) -> None:
        """The OWNED producer registers EVERY object it mints. The
        STRONG reference pins id(obj) for the store's lifetime - a
        garbage-collected producer's id can NEVER be inherited by an
        unrelated later object (B5: id-only registries are unsound
        under CPython id recycling)."""
        self._minted[id(obj)] = obj

    def _wrap(self, inner, binding_hash: str):
        """wrap(cap|claim|capture) -> SessionBound*. The store wraps ONLY
        objects IT minted this session via the OWNED producer."""
        if id(inner) not in self._minted:
            raise cap.AuthError()  # MISSING_IDENTITY - unproduced/copy
        if type(inner) is cap.Capability:
            return SessionBoundCapability(
                session_id=self._session_id,
                handle=self._register_handle(inner, binding_hash),
                inner=inner, binding_hash=binding_hash)
        if type(inner) is env.ModelOnlyClaimResult:
            return SessionBoundClaim(
                session_id=self._session_id,
                handle=self._register_handle(inner, binding_hash),
                inner=inner, binding_hash=binding_hash)
        if type(inner) is env.ModelOnlyVerifiedCapture:
            return SessionBoundCapture(
                session_id=self._session_id,
                handle=self._register_handle(inner, binding_hash),
                inner=inner, binding_hash=binding_hash)
        raise cap.AuthError()  # MISSING_IDENTITY - unproduced object

    def mint_mutation_fixtures(self, request: FactRequest):
        """The MUTATION producer call: returns (wrapped capability, wrapped
        claim, wrapped capture, binding). The caller NEVER supplies
        identity/lifetime/scopes/times/revisions/state/token/fence."""
        if type(request) is not FactRequest:
            raise cap.ModelInputError()
        capability = self._producer.mint_mutation(request)
        binding = self._producer.build_binding(request, capability)
        binding_hash = extended_binding_hash(binding, request)
        capture, claim = self._producer.seal_and_claim(binding)
        self._register_minted(capability)
        self._register_minted(capture)
        self._register_minted(claim)
        return (self._wrap(capability, binding_hash),
                self._wrap(claim, binding_hash),
                self._wrap(capture, binding_hash), binding)

    def mint_read_fixtures(self, owner_uid: int, lifetime_id: int,
                           operation_id: str, method: ReadMethod,
                           targets: dict):
        """The READ producer call: returns (wrapped capability, context).
        The projection binds the store's OWNED current typed context
        (issuer/snapshot from the owned fixtures - never a literal or a
        caller-asserted value); it is REGISTERED and re-used at call
        time, so a valid later call never miscompares on clock drift."""
        _req_int(owner_uid, 0)
        _req_int(lifetime_id, 1)
        _req_hex(operation_id, OP_ID_LEN)
        _req_enum(method, ReadMethod)
        if type(targets) is not dict:
            raise cap.ModelInputError()
        capability = self._producer.mint_read(owner_uid, lifetime_id,
                                              operation_id, method)
        self._register_minted(capability)
        read_binding = self._producer.build_read_binding(
            owner_uid, lifetime_id, operation_id, method)
        context = self._caller_context(read_binding)
        caller_canon = json.loads(_canon(_context_canon(context)))
        clock_reading = _clock_now(self._clock)
        handle = secrets.token_hex(16)  # minted FIRST so the hash binds it
        binding_hash = _read_projection(self, method, owner_uid,
                                        lifetime_id, targets,
                                        clock_reading, caller_canon,
                                        handle)
        wrapped = SessionBoundCapability(
            session_id=self._session_id, handle=handle,
            inner=capability, binding_hash=binding_hash)
        self._handles[handle] = (capability, binding_hash)
        self._read_contexts[wrapped.handle] = {
            "caller": caller_canon, "clock_reading": clock_reading}
        return wrapped, context

    # -- Phase helpers -------------------------------------------------------

    def _phase_a(self, capability: "cap.Capability", request: FactRequest,
                 operation_class, scopes, captured_epoch) -> None:
        """PHASE A IS the ACTUAL PR-3 evaluator call (sec 8): tier order
        runs UNCHANGED. A refusal here means NO execution of any kind."""
        cap_request = cap.Request(
            operation_id=request.operation_id,
            operation_class=operation_class,
            required_scopes=frozenset(scopes), captured_epoch=captured_epoch,
            requested_audience_id=None,
            permitted_information_ref=request.information_ref,
            extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
            group_action=None, sensitive=False)
        decision = self._evaluator.recheck_model(capability, cap_request)
        if not decision.allowed:
            raise cap.ConflictError(decision.code) if decision.code in (
                cap.ModelCode.REVOKED, cap.ModelCode.STALE_POLICY,
                cap.ModelCode.EPOCH, cap.ModelCode.REQUEST_DRIFT) else \
                _phase_a_error(decision.code)

    def _store_revision(self, uid: int) -> int:
        row = self._q.q("SELECT store_revision FROM store_revisions"
                        " WHERE uid=%s", (uid,), "one")
        return int(row["store_revision"]) if row is not None else 0

    def _bump_revision(self, uid: int) -> int:
        current = self._store_revision(uid)
        new = _checked_add(current, 1)
        self._q.q("INSERT INTO store_revisions(uid, store_revision)"
                  " VALUES(%s,%s) ON CONFLICT(uid) DO UPDATE SET"
                  " store_revision=excluded.store_revision RETURNING"
                  " store_revision", (uid, new), "one")
        return new

    def _current_epoch(self, uid: int) -> int:
        row = self._q.q("SELECT current_epoch FROM fixture_snapshot"
                        " WHERE uid=%s", (uid,), "one")
        if row is None:
            raise cap.AuthError()
        return int(row["current_epoch"])

    # -- execute (sec 10 - ONE transaction, BEGIN IMMEDIATE) -----------------

    def execute(self, owner_uid: int, request: FactRequest,
                capability: SessionBoundCapability,
                claim: SessionBoundClaim,
                capture: SessionBoundCapture,
                context: "env.ModelOnlyCallerContext") -> ExecuteResult:
        _req_int(owner_uid, 0)
        if type(request) is not FactRequest:
            raise cap.ModelInputError()
        if type(context) is not env.ModelOnlyCallerContext:
            raise cap.ModelInputError()
        # FIRST check on every public method: handle validation
        capability_inner = self._validate_handle(
            capability, SessionBoundCapability, "capability")
        claim_inner = self._validate_handle(claim, SessionBoundClaim, "claim")
        capture_inner = self._validate_handle(
            capture, SessionBoundCapture, "capture")
        binding = capture_inner.payload.binding
        binding_hash = extended_binding_hash(binding, request)
        if request.owner_uid != owner_uid:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)

        conn = self._q.raw
        uid, lid = owner_uid, request.lifetime_id
        op_id = request.operation_id
        # TXN 1 (ONE atomic transaction): Phase A + the registered-op
        # readback (the launch gate's clean-state establishment) and,
        # for a NEW execution ONLY, the witness ARM + registry claim.
        # NO execution step runs before the claim commits (sec 5 race
        # vectors). A refusal here is PRE-EXECUTION: nothing claimed or
        # armed, so an UNCHANGED retry is permitted; the refused
        # attempt row records the refusal and NEVER blocks (sec 5).
        conn.execute("BEGIN IMMEDIATE")
        attempt_id = None
        try:
            pre = self._execute_readback(owner_uid, request,
                                         capability_inner, claim_inner,
                                         capture_inner, binding,
                                         binding_hash, context,
                                         capability.binding_hash,
                                         claim.binding_hash,
                                         capture.binding_hash)
            if pre is None:
                attempt_id = secrets.token_hex(16)  # FRESH per attempt
                self._arm_and_claim(uid, lid, request, attempt_id,
                                    binding_hash)
            conn.execute("COMMIT")
        except Exception as exc:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            self._record_refused_attempt(uid, lid, op_id, binding_hash,
                                         type(exc).__name__)
            raise
        if pre is not None:
            return pre
        # TXN 2: the 9-step commit. The COMMIT GUARD consumes the armed
        # witness + clears the registry IN THE SAME transaction.
        conn.execute("BEGIN IMMEDIATE")
        try:
            # FRESH Phase A + current checks INSIDE the commit
            # transaction (v80 sec 10: current checks + effects = ONE
            # commit tx; R3): a mid-flight revocation/epoch move
            # refuses HERE, before any effect
            self._execute_current_checks(
                owner_uid, request, capability_inner, claim_inner,
                binding, binding_hash, context, capability.binding_hash,
                claim.binding_hash, capture.binding_hash)
            result = self._execute_apply(uid, lid, request, claim_inner,
                                         binding, binding_hash)
            self._commit_guard(op_id, attempt_id,
                                 self._session_id)
            if self._fault_commit_lost:
                raise _CommitUncertain()
        except _CommitUncertain:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return self._commit_uncertain(owner_uid, request, binding_hash)
        except _CommitGuardAbort:
            # fenced/owned by ANOTHER attempt: ABORT; the operation is
            # resolver-only and its outcome stays UNKNOWN to this caller
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return ExecuteResult(
                kind="local_uncertain", operation_id=op_id,
                owner=Lifetime(uid=uid, lifetime_id=lid),
                binding_hash=binding_hash)
        except Exception as exc:
            # a REFUSAL rolls back the ENTIRE attempt (sec 5): unwind
            # the claim (disarm + drop the registry row) so an UNCHANGED
            # retry stays permitted, then record the refused attempt row
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            self._unwind_claim(op_id, attempt_id)
            self._record_refused_attempt(uid, lid, op_id, binding_hash,
                                         type(exc).__name__)
            raise
        try:
            conn.execute("COMMIT")
        except Exception:
            # COMMIT outcome UNCERTAIN (sec 9): NEVER assumed rolled
            # back - a COMMIT exception MAY have written
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            return self._commit_uncertain(owner_uid, request, binding_hash)
        # READBACK (same step): the REACQUIRED post-commit read (sec 10)
        return self._post_commit_read(owner_uid, request, result)

    def _execute_current_checks(self, owner_uid, request, capability,
                                claim, binding, binding_hash, context,
                                wrapper_cap_hash, wrapper_claim_hash,
                                wrapper_capture_hash):
        """The CURRENT-state checks (sec 10): fresh issuer re-read + the
        REAL PR-3 Phase A recheck + association/equality re-checks.
        Run in TXN 1 (the launch gate) AND AGAIN inside the commit
        transaction (TXN 2) - current checks + effects land in ONE
        commit tx (v80 sec 10; R3)."""
        q = self._q
        uid, lid = owner_uid, request.lifetime_id
        # (step 2) CURRENT fixture + issuer re-read FIRST, then the REAL PR-3
        # recheck against that CURRENT snapshot (Phase A)
        # CURRENT issuer re-read: the table row (NOT the caller's
        # flag) is the validity proof; the re-read typed issuer must
        # EQUAL the context's current_issuer; current_issuer_valid
        # False = STALE (sec 4, V55)
        issuer_row = q.q("SELECT issuer_json, valid FROM issuer_state"
                         " WHERE uid=%s", (uid,), "one")
        if issuer_row is None or not issuer_row["valid"] \
                or not context.current_issuer_valid:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        stored_issuer = _typed_issuer_from_json(issuer_row["issuer_json"])
        if stored_issuer is not None \
                and stored_issuer != context.current_issuer:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        # ORIGINAL == BOUND (T1): the supplied context's ORIGINAL
        # envelope must BE this request's actual binding - a foreign
        # original envelope under a fresh current context is not this
        # operation's envelope
        if context.original_binding != binding:
            raise cap.AuthError()
        # ORIGINAL issuer == CURRENT issuer (T1): the ORIGINAL
        # envelope NEVER executes after the issuer changed, even with
        # a new valid current context (the R2 read-side rule applied
        # to mutations)
        if context.original_binding.issuer != context.current_issuer:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        operation_class = _METHOD_OPERATION[request.method]
        scopes = _METHOD_SCOPES[request.method]
        # the CAPTURED epoch is the capability's immutable mint-time value
        # (never a fresh read): PR-3 tier 4 owns the epoch comparison, so
        # a post-erasure replay refuses EPOCH, never REQUEST_DRIFT (V31)
        captured_epoch = capability.request.captured_epoch
        self._phase_a(capability, request, operation_class, scopes,
                      captured_epoch)
        # CURRENT OWNED equality (T1, AFTER Phase A so the PR-3 tier
        # precedence - expiry/revocation/epoch - is never masked): the
        # caller's claimed CURRENT snapshot fields must EQUAL the
        # store's OWNED snapshot re-read HERE inside the transaction;
        # a fabricated or stale field (uid/lifetime 999, grant
        # generation 999, capture epoch 999, policy 999) is typed
        # STALE, never executed
        snap = self._snapshots.read(uid)
        if context.current_uid != uid \
                or context.current_lifetime != lid \
                or context.current_capture_epoch != snap.current_epoch \
                or context.current_grant_generation \
                != snap.grants_generation \
                or context.current_policy_revision != snap.policy_revision \
                or context.current_decision_revision \
                != snap.decision_revision \
                or context.current_consent_generation \
                != snap.consent_generation:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        # (step 3a) association/equality re-checks run as their OWN
        # declared seam: the post-step-2/post-step-3a fault boundary is
        # EXACT (v80 3928 after-each-step coverage)
        self._execute_association_checks(
            owner_uid, request, capability, claim, binding, binding_hash,
            wrapper_cap_hash, wrapper_claim_hash, wrapper_capture_hash)

    def _execute_association_checks(self, owner_uid, request, capability,
                                    claim, binding, binding_hash,
                                    wrapper_cap_hash, wrapper_claim_hash,
                                    wrapper_capture_hash):
        """(step 3a) association/equality re-checks (EVERY path) - the
        three-bundle hash agreement runs AFTER Phase A: the real PR-3
        precedence is A-before-B, so expired/epoch-stale/revoked refuses
        in Phase A FIRST (V31 - NO universal DRIFT-over-EPOCH)."""
        q = self._q
        uid, lid = owner_uid, request.lifetime_id
        if binding_hash != wrapper_cap_hash \
                or binding_hash != wrapper_claim_hash \
                or binding_hash != wrapper_capture_hash:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        if capability.identity.owner_uid != uid \
                or capability.identity.lifetime_id != lid:
            raise cap.AuthError()
        if binding.uid != uid or binding.lifetime != lid \
                or binding.operation_id != request.operation_id \
                or binding.information_ref != request.information_ref:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        if claim.scope.uid != uid or claim.scope.lifetime != lid \
                or claim.scope.operation_id != request.operation_id:
            raise cap.AuthError()
        if q.q("SELECT uid FROM owners WHERE uid=%s AND lifetime_id=%s",
               (uid, lid), "one") is None:
            raise cap.AuthError()

    def _execute_readback(self, owner_uid, request, capability, claim,
                          capture, binding, binding_hash, context,
                          wrapper_cap_hash, wrapper_claim_hash,
                          wrapper_capture_hash):
        q = self._q
        uid, lid = owner_uid, request.lifetime_id
        # STEP 1a: ordered footprint (SQLite serializes at BEGIN IMMEDIATE;
        # the footprint order is declared, per-table locks are a no-op here)
        _footprint(request)
        # STEPs 2-3a: the CURRENT checks (fresh issuer re-read + the real
        # PR-3 Phase A recheck + association/equality re-checks)
        self._execute_current_checks(
            owner_uid, request, capability, claim, binding, binding_hash,
            context, wrapper_cap_hash, wrapper_claim_hash,
            wrapper_capture_hash)
        # STEP 4 + STEP 3b: the atomic registered-operation readback
        # (PHASE B) and the ACQUIRED execution gate
        return self._phase_b_lookup(uid, lid, request, binding_hash,
                                    claim)

    def _phase_b_lookup(self, uid, lid, request, binding_hash, claim):
        """STEP 4: ONE ATOMIC registered-operation readback (PHASE B);
        STEP 3b: the ACQUIRED execution gate (NEW execution ONLY).
        Returns the typed replay/status result, or None on a CLEAN
        state (the caller arms + claims, then applies)."""
        q = self._q
        op_id = request.operation_id
        record = q.q("SELECT * FROM operation_records WHERE uid=%s AND"
                     " lifetime_id=%s AND operation_id=%s",
                     (uid, lid, op_id), "one")
        blocked = q.q("SELECT * FROM blocked_records WHERE uid=%s AND"
                      " lifetime_id=%s AND operation_id=%s",
                      (uid, lid, op_id), "one")
        registry_row = q.q("SELECT * FROM attempt_registry WHERE"
                           " operation_id=%s", (op_id,), "one")
        armed = q.q("SELECT * FROM abort_witnesses WHERE operation_id=%s"
                    " AND state='armed'", (op_id,), "all")
        resolved = q.q("SELECT * FROM resolved_accounting WHERE uid=%s AND"
                       " lifetime_id=%s AND operation_id=%s",
                       (uid, lid, op_id), "one")
        local = self._uncertain.get((uid, lid, op_id))
        if record is not None and record["binding_hash"] != binding_hash:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        committed = record is not None and record["marker"] is not None \
            and record["outcome_json"] is not None
        if committed:
            # COMMITTED: EXACT RECORDED REPLAY ONLY
            outcome = _decode_stored_outcome(record)
            kind = "outcome" if type(outcome) is OperationOutcome \
                else "scrubbed"
            return ExecuteResult(kind="replayed",
                                 replay=ReplayResult(kind=kind,
                                                     outcome=outcome))
        if blocked is not None or registry_row is not None or armed \
                or resolved is not None or local is not None \
                or record is not None:
            # registered-but-UNCOMMITTED: typed STATUS, resolve ONLY
            if blocked is not None:
                return ExecuteResult(kind="durable_blocked",
                                     record=_blocked_from_row(blocked))
            return ExecuteResult(
                kind="local_uncertain", operation_id=op_id,
                owner=Lifetime(uid=uid, lifetime_id=lid),
                binding_hash=binding_hash)
        # STEP 3b: the ACQUIRED execution gate (NEW execution ONLY)
        if claim.claim_state is not env.ClaimState.ACQUIRED \
                or claim.owner_token is None or claim.fence is None \
                or claim.fence < 1:
            raise cap.AuthError()
        return None  # CLEAN state: the caller arms + claims, then applies

    def _arm_and_claim(self, uid, lid, request, attempt_id, binding_hash):
        """The ONE atomic claim (sec 5): a FRESH attempt_id
        (secrets.token_hex(16)); the witness is ARMED for THAT
        (operation_id, attempt_id) AND the attempt registered as the
        ACTIVE owner in attempt_registry IN THE SAME transaction that
        established the clean state."""
        now = _clock_now(self._clock)
        op_id = request.operation_id
        self._q.q("INSERT INTO abort_witnesses(operation_id, attempt_id,"
                  " uid, lifetime_id, commitment, fence, session_id,"
                  " state, terminal_state, attested_at,"
                  " resolution_event_id, written_at)"
                  " VALUES(%s,%s,%s,%s,%s,1,%s,'armed',NULL,NULL,NULL,%s)",
                  (op_id, attempt_id, uid, lid, binding_hash,
                   self._session_id, now), "exec")
        self._q.q("INSERT INTO attempt_registry(operation_id, uid,"
                  " lifetime_id, attempt_id, session_id, commitment,"
                  " state, fenced_by, fenced_at, resolution_event_id,"
                  " minted_at) VALUES(%s,%s,%s,%s,%s,%s,'active',NULL,"
                  " NULL,NULL,%s)",
                  (op_id, uid, lid, attempt_id, self._session_id,
                   binding_hash, now), "exec")

    def _commit_guard(self, op_id, attempt_id, session_id):
        """COMMIT GUARD (v80 626-630 fencing/serialization, EXACT): the
        9-step commit transaction of attempt A MUST find (1) witness(A)
        'armed' AND (2) the attempt_registry row in state 'active'
        OWNED BY A (attempt_id == A's AND session_id == A's) - then
        CONSUMES the witness AND clears the registry IN THE SAME
        transaction. ANY other state (missing/attested/foreign/a row
        owned by ANOTHER SESSION) ABORTS: once a witness leaves
        'armed', that attempt can NEVER commit."""
        cur = self._q.raw.execute(
            "UPDATE abort_witnesses SET state='consumed' WHERE"
            " operation_id=? AND attempt_id=? AND session_id=? AND"
            " state='armed'", (op_id, attempt_id, session_id))
        if cur.rowcount != 1:
            raise _CommitGuardAbort()
        cur = self._q.raw.execute(
            "DELETE FROM attempt_registry WHERE operation_id=? AND"
            " attempt_id=? AND session_id=? AND state='active'",
            (op_id, attempt_id, session_id))
        if cur.rowcount != 1:
            raise _CommitGuardAbort()

    def _unwind_claim(self, op_id, attempt_id) -> None:
        """Refusal unwind: delete THIS attempt's armed witness + active
        registry row (best-effort, own transaction). Uncertainty paths
        NEVER call this - an uncertain attempt stays armed for the
        resolver."""
        try:
            self._q.raw.execute("BEGIN IMMEDIATE")
            self._q.raw.execute(
                "DELETE FROM abort_witnesses WHERE operation_id=? AND"
                " attempt_id=? AND state='armed'", (op_id, attempt_id))
            self._q.raw.execute(
                "DELETE FROM attempt_registry WHERE operation_id=? AND"
                " attempt_id=? AND state='active'", (op_id, attempt_id))
            self._q.raw.execute("COMMIT")
        except Exception:
            try:
                self._q.raw.execute("ROLLBACK")
            except Exception:
                pass

    def _record_refused_attempt(self, uid, lid, op_id, binding_hash,
                                refusal: str) -> None:
        """A REFUSED PRE-EXECUTION attempt row (sec 5): it NEVER blocks -
        a refused attempt never claimed the registry, never armed a
        witness, never wrote a record; an UNCHANGED retry is PERMITTED."""
        now = _clock_now(self._clock)
        try:
            self._q.raw.execute("BEGIN IMMEDIATE")
            row = self._q.q("SELECT COALESCE(MAX(attempt_seq),0)+1 AS s"
                            " FROM attempt_records WHERE uid=%s AND"
                            " lifetime_id=%s AND operation_id=%s",
                            (uid, lid, op_id), "one")
            self._q.q("INSERT INTO attempt_records(uid, lifetime_id,"
                      " operation_id, attempt_seq, attempt_binding_hash,"
                      " refusal, created_at)"
                      " VALUES(%s,%s,%s,%s,%s,%s,%s)",
                      (uid, lid, op_id, int(row["s"]), binding_hash,
                       refusal[:BOUNDED_STR_MAX], now), "exec")
            self._q.raw.execute("COMMIT")
        except Exception:
            try:
                self._q.raw.execute("ROLLBACK")
            except Exception:
                pass

    def _post_commit_read(self, owner_uid, request, result):
        """READBACK (sec 10, same step): COMMIT + the REACQUIRED
        post-commit read bound to owner + fence + scope + outcome.
        COMMITTED + read FAILS = 'unavailable' with the typed cause:
        ReadbackUnavailableError carries the KNOWN outcome + revision;
        the commit certainty is UNCHANGED - never uncertain, never
        rolled back, never retried."""
        if result.kind != "executed":
            return result
        outcome = result.result.outcome
        known_revision = result.result.receipt.store_revision
        try:
            if self._fault_readback_cause is not None:
                raise _InjectedReadbackFailure()
            self._q.raw.execute("BEGIN IMMEDIATE")
            try:
                current = self._current_view(owner_uid,
                                             request.lifetime_id, outcome)
                revision = max(self._store_revision(owner_uid), 1)
                self._q.raw.execute("COMMIT")
            except Exception:
                try:
                    self._q.raw.execute("ROLLBACK")
                except Exception:
                    pass
                raise
        except _InjectedReadbackFailure:
            raise ReadbackUnavailableError(outcome, known_revision,
                                           self._fault_readback_cause)
        except sqlite3.Error as exc:
            raise ReadbackUnavailableError(outcome, known_revision,
                                           ReadbackCause.OPERATIONAL)                 from exc
        except Exception as exc:
            raise ReadbackUnavailableError(outcome, known_revision,
                                           ReadbackCause.PROTOCOL) from exc
        return ExecuteResult(
            kind="executed", result=result.result,
            post=PostReadResult(
                original_outcome=outcome,
                applied_at_revision=outcome.applied_at_revision,
                current_revision=revision, current=current))

    def _execute_apply(self, uid, lid, request, claim, binding,
                       binding_hash):
        """TXN 2 (the 9-step commit): steps 5-9. Runs ONLY after the
        claim commits - no execution step precedes the claim (sec 5)."""
        # STEP 5: tombstone check (put/supersede ONLY)
        if request.method in (MethodCode.PUT, MethodCode.SUPERSEDE):
            self._tombstone_check(uid, lid, request)
        # STEP 6: CAS
        self._cas_check(uid, lid, request)
        # STEP 7: resulting-count predicate + collision classification
        outcome_plan = self._classify(uid, lid, request)
        # STEP 8: monotone allocation ONLY for epoch-setting events, AFTER
        # all validation, IMMEDIATELY BEFORE the effect writes
        new_epoch = None
        if outcome_plan["allocates_epoch"]:
            new_epoch = owner_counters.allocate_next_epoch(self._q, uid)
            # erasure SETs memory_epoch (sec 3): the owned fixture snapshot
            # reflects the allocation atomically
            self._q.q("UPDATE fixture_snapshot SET current_epoch=%s"
                      " WHERE uid=%s", (new_epoch, uid), "exec")
        # STEP 9: apply writes + record + proof, then READBACK
        return self._apply_and_record(uid, lid, request, claim, binding,
                                      binding_hash, outcome_plan, new_epoch)

    def _commit_uncertain(self, owner_uid, request,
                          binding_hash) -> ExecuteResult:
        """COMMIT outcome UNCERTAIN (sec 9): try the durable block marker;
        a failed write = LOCAL_UNCERTAIN (in-memory only)."""
        uid, lid, op_id = (owner_uid, request.lifetime_id,
                           request.operation_id)
        recovery_id = _recovery_id(uid, lid, op_id, binding_hash)
        now = _clock_now(self._clock)
        try:
            self._q.raw.execute("BEGIN IMMEDIATE")
            self._q.q(
                "INSERT INTO blocked_records(recovery_id, uid, lifetime_id,"
                " operation_id, binding_hash, proof, resolver, deadline,"
                " retention, state, evidence_count, overflow_marker,"
                " created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,'blocked',"
                " 0,0,%s)",
                (recovery_id, uid, lid, op_id, binding_hash, "{}",
                 ResolverCode.MANUAL.value, now + 86400,
                 RetentionCode.STANDARD.value, now), "exec")
            # the attempt witness stays ARMED and the registry row
            # ACTIVE (armed+claimed in TXN 1): the resolver fences +
            # attests them - the durable block marker NEVER consumes
            self._q.raw.execute("COMMIT")
            row = self._q.q("SELECT * FROM blocked_records WHERE"
                            " recovery_id=%s", (recovery_id,), "one")
            return ExecuteResult(kind="durable_blocked",
                                 record=_blocked_from_row(row))
        except Exception:
            try:
                self._q.raw.execute("ROLLBACK")
            except Exception:
                pass
            self._uncertain[(uid, lid, op_id)] = {
                "binding_hash": binding_hash, "at": now}
            return ExecuteResult(
                kind="local_uncertain", operation_id=op_id,
                owner=Lifetime(uid=uid, lifetime_id=lid),
                binding_hash=binding_hash)


class _CommitUncertain(Exception):
    """Internal: commit-outcome uncertainty injection (test fault hook)."""


class _CommitGuardAbort(Exception):
    """Internal: the commit guard found the witness/registry fenced or
    owned by ANOTHER attempt - the commit ABORTS (sec 5)."""


class _InjectedReadbackFailure(Exception):
    """Internal: post-commit read failure injection (test fault hook)."""


def _phase_a_error(code: "cap.ModelCode"):
    mapping = {
        cap.ModelCode.MALFORMED_INPUT: cap.ModelInputError,
        cap.ModelCode.MISSING_IDENTITY: cap.AuthError,
        cap.ModelCode.TIER_DISABLED: cap.SensitiveRefused,
        cap.ModelCode.EXPIRED: cap.ExpiredRefused,
        cap.ModelCode.MISSING_SCOPE: cap.ScopeRefused,
        cap.ModelCode.MISSING_CONSENT: cap.ConsentRefused,
        cap.ModelCode.POLICY_REFUSED: cap.PolicyRefused,
        cap.ModelCode.REVOKED: lambda: cap.ConflictError(
            cap.ModelCode.REVOKED),
        cap.ModelCode.STALE_POLICY: lambda: cap.ConflictError(
            cap.ModelCode.STALE_POLICY),
        cap.ModelCode.EPOCH: lambda: cap.ConflictError(cap.ModelCode.EPOCH),
        cap.ModelCode.REQUEST_DRIFT: lambda: cap.ConflictError(
            cap.ModelCode.REQUEST_DRIFT),
        cap.ModelCode.MODEL_ONLY: cap.ProductionStubRefused,
    }
    factory = mapping.get(code, cap.ModelInputError)
    return factory() if callable(factory) else factory


def _blocked_from_row(row) -> BlockedRecord:
    return BlockedRecord(
        recovery_id=row["recovery_id"], uid=row["uid"],
        lifetime_id=row["lifetime_id"], operation_id=row["operation_id"],
        binding_hash=row["binding_hash"], proof=row["proof"],
        resolver=ResolverCode(row["resolver"]), deadline=row["deadline"],
        retention=RetentionCode(row["retention"]), state=row["state"],
        evidence_count=row["evidence_count"],
        overflow_marker=row["overflow_marker"], created_at=row["created_at"])


def _decode_stored_outcome(record):
    """ONE decode rule (sec 7): outcome_kind selects; the scrubbed flag
    column must AGREE (coherence-checked)."""
    kind = record["outcome_kind"]
    scrubbed_flag = record["scrubbed"]
    raw = record["outcome_json"]
    if raw is None:
        raise cap.ModelInputError()
    obj = json.loads(raw)
    if kind == "full":
        if scrubbed_flag != 0:
            raise cap.ModelInputError()
        return outcome_from_dict(obj)
    if kind == "scrubbed":
        if scrubbed_flag != 1:
            raise cap.ModelInputError()
        return scrubbed_from_dict(obj)
    raise cap.ModelInputError()


def _footprint(request: FactRequest) -> tuple:
    """STEP 1a's ordered footprint (table-name order, per-operation union)."""
    tables = {"attempt_records", "attempt_registry", "abort_witnesses",
              "blocked_records", "facts", "history", "id_allocators",
              "operation_records", "operation_receipts", "store_revisions",
              "stored_requests"}
    if request.method in (MethodCode.PUT, MethodCode.SUPERSEDE):
        tables |= {"markers", "marker_keys", "fact_lineage", "lineage_ops",
                   "lineage_copies"}
    if request.method is MethodCode.DELETE:
        tables |= {"copy_content", "copy_inventory", "erasure_identities",
                   "erasure_lineage_copies", "erasure_lineage_ops",
                   "fact_lineage", "lineage_copies", "lineage_ops",
                   "report_manifest", "report_pages"}
    if request.method is MethodCode.WIPE:
        tables |= {"copy_content", "copy_inventory", "cursor_registry",
                   "erasure_identities", "erasure_lineage_copies",
                   "erasure_lineage_ops", "fact_lineage", "lineage_copies",
                   "lineage_ops", "optins", "report_manifest",
                   "report_pages"}
    if request.method is MethodCode.SET_OPTIN:
        tables |= {"optins"}
    return tuple(sorted(tables))


# ---------------------------------------------------------------------------
# Steps 5-8 helpers + effect writers (FactStore methods)
# ---------------------------------------------------------------------------

def _fact_row_to_readback(row) -> FactReadback:
    return FactReadback(
        fact_id=row["fact_id"], key=row["key"], value=row["value"],
        category=Category(row["category"]), source=Source(row["source"]),
        provenance=Provenance(excerpt=row["excerpt"],
                              captured_at=row["captured_at"]),
        updated_at=row["updated_at"], row_version=row["row_version"])


class _Effects:
    """Mixin: step 5-9 effect machinery on FactStore."""

    def _tombstone_check(self, uid, lid, request) -> None:
        """STEP 5 (put/supersede ONLY): the ONE current-owner-epoch formula -
        refuse ERASED IFF a PRE-EXISTING marker matches owner + lifetime +
        marker_class + key_ref AND marker.generation <= the CURRENT owner
        epoch at the check instant."""
        fact = request.payload.fact if request.method is MethodCode.PUT \
            else request.payload.replacement
        norm = _normalize_key(fact.key)
        epoch = self._current_epoch(uid)
        keys = self._q.q("SELECT fixture_key FROM marker_keys WHERE uid=%s"
                         " AND lifetime_id=%s", (uid, lid), "all")
        for key_row in keys:
            key_ref = _hmac.new(key_row["fixture_key"].encode("utf-8"),
                                norm.encode("utf-8"),
                                hashlib.sha256).hexdigest()
            marker = self._q.q(
                "SELECT generation FROM markers WHERE marker_key=%s AND"
                " uid=%s AND lifetime_id=%s AND key_ref=%s",
                (key_row["fixture_key"], uid, lid, key_ref), "one")
            if marker is not None and marker["generation"] <= epoch:
                raise FactConflict("ERASED")

    def _cas_check(self, uid, lid, request) -> None:
        """STEP 6: CAS bound to owner + lifetime + versions."""
        q = self._q
        if request.method is MethodCode.SUPERSEDE:
            for fid, expected in sorted(
                    request.payload.expected_versions.items()):
                row = q.q("SELECT row_version FROM facts WHERE uid=%s AND"
                          " lifetime_id=%s AND fact_id=%s",
                          (uid, lid, fid), "one")
                if row is None or row["row_version"] != expected:
                    raise FactConflict("CAS")
        elif request.method is MethodCode.WIPE:
            for fid, expected in sorted(
                    request.payload.expected_target_versions.items()):
                row = q.q("SELECT row_version FROM facts WHERE uid=%s AND"
                          " lifetime_id=%s AND fact_id=%s",
                          (uid, lid, fid), "one")
                if row is None or row["row_version"] != expected:
                    raise FactConflict("CAS")
        elif request.method is MethodCode.DELETE:
            payload = request.payload
            live = q.q("SELECT row_version FROM facts WHERE uid=%s AND"
                       " lifetime_id=%s AND fact_id=%s",
                       (uid, lid, payload.fid), "one")
            live_version = live["row_version"] if live is not None else None
            if live_version != payload.closure_cas.live:
                raise FactConflict("CAS")
            manifest = self._compute_closure_manifest(uid, lid, payload.fid,
                                                      live_version)
            if manifest.lineage_digest() != \
                    payload.closure_cas.lineage_digest:
                raise FactConflict("CAS")

    def _classify(self, uid, lid, request) -> dict:
        """STEP 7: resulting-count predicate + collision classification
        (fresh put/supersede ONLY, AFTER lookup/classification)."""
        plan = {"allocates_epoch": request.method in (MethodCode.DELETE,
                                                      MethodCode.WIPE),
                "kind": "apply"}
        q = self._q
        if request.method is MethodCode.PUT:
            fact = request.payload.fact
            norm = _normalize_key(fact.key)
            existing = q.q("SELECT * FROM facts WHERE uid=%s AND"
                           " lifetime_id=%s AND key=%s", (uid, lid, norm),
                           "one")
            if existing is not None:
                diffs = []
                if existing["value"] != fact.value:
                    diffs.append(ConflictField.VALUE)
                if existing["category"] != fact.category.value:
                    diffs.append(ConflictField.CATEGORY)
                if existing["source"] != fact.source.value:
                    diffs.append(ConflictField.SOURCE)
                if (existing["excerpt"] != fact.provenance.excerpt
                        or existing["captured_at"] !=
                        fact.provenance.captured_at):
                    diffs.append(ConflictField.PROVENANCE)
                if diffs:
                    plan["kind"] = "conflict"
                    plan["conflict_fields"] = tuple(sorted(
                        set(diffs), key=lambda f: f.value))
                else:
                    plan["kind"] = "unchanged"
                return plan
            count = q.q("SELECT COUNT(*) AS n FROM facts WHERE uid=%s AND"
                        " lifetime_id=%s", (uid, lid), "one")["n"]
            if count + 1 > LIVE_FACT_CAP:
                raise cap.CapRefused(count + 1)
            return plan
        if request.method is MethodCode.SUPERSEDE:
            payload = request.payload
            norm = _normalize_key(payload.replacement.key)
            collision = q.q("SELECT fact_id FROM facts WHERE uid=%s AND"
                            " lifetime_id=%s AND key=%s",
                            (uid, lid, norm), "one")
            if collision is not None and collision["fact_id"] not in \
                    set(payload.old_fids):
                plan["kind"] = "conflict"
                plan["collided_fid"] = collision["fact_id"]
                return plan
            count = q.q("SELECT COUNT(*) AS n FROM facts WHERE uid=%s AND"
                        " lifetime_id=%s", (uid, lid), "one")["n"]
            resulting = count - len(payload.old_fids) + 1
            if resulting > LIVE_FACT_CAP:
                raise cap.CapRefused(resulting)
            return plan
        if request.method is MethodCode.DELETE:
            payload = request.payload
            live = q.q("SELECT row_version FROM facts WHERE uid=%s AND"
                       " lifetime_id=%s AND fact_id=%s",
                       (uid, lid, payload.fid), "one")
            history = q.q("SELECT 1 AS x FROM history WHERE uid=%s AND"
                          " lifetime_id=%s AND fact_id=%s LIMIT 1",
                          (uid, lid, payload.fid), "one")
            if live is None and history is None:
                plan["kind"] = "absent"
                plan["allocates_epoch"] = False
            return plan
        return plan

    def _alloc_fact_id(self, uid, lid) -> int:
        row = self._q.q(
            "INSERT INTO id_allocators(uid, lifetime_id, next_fact_id,"
            " next_history_id) VALUES(%s,%s,2,1) ON CONFLICT(uid,"
            " lifetime_id) DO UPDATE SET next_fact_id=next_fact_id+1"
            " RETURNING next_fact_id", (uid, lid), "one")
        return int(row["next_fact_id"]) - 1

    def _alloc_history_id(self, uid, lid) -> int:
        row = self._q.q(
            "INSERT INTO id_allocators(uid, lifetime_id, next_fact_id,"
            " next_history_id) VALUES(%s,%s,1,2) ON CONFLICT(uid,"
            " lifetime_id) DO UPDATE SET next_history_id=next_history_id+1"
            " RETURNING next_history_id", (uid, lid), "one")
        return int(row["next_history_id"]) - 1

    def _apply_and_record(self, uid, lid, request, claim, binding,
                          binding_hash, plan, new_epoch) -> ExecuteResult:
        """STEP 9: apply writes + record + proof, then READBACK."""
        now = _clock_now(self._clock)
        op_id = request.operation_id
        revision = self._store_revision(uid)
        outcome = None
        if plan["kind"] in ("conflict", "unchanged", "absent"):
            outcome = self._record_only_outcome(request, plan)
        elif request.method is MethodCode.PUT:
            outcome, revision = self._apply_put(uid, lid, request, now)
        elif request.method is MethodCode.SUPERSEDE:
            outcome, revision = self._apply_supersede(uid, lid, request,
                                                      now, binding_hash)
        elif request.method is MethodCode.SET_OPTIN:
            outcome, revision = self._apply_set_optin(uid, lid, request,
                                                      now)
        elif request.method is MethodCode.DELETE:
            outcome, revision = self._apply_delete(uid, lid, request, now,
                                                   binding_hash, new_epoch)
        elif request.method is MethodCode.WIPE:
            outcome, revision = self._apply_wipe(uid, lid, request, now,
                                                 new_epoch)
        # OperationRecord written in THIS same transaction (COMMITTED iff
        # marker non-NULL AND outcome_json non-NULL)
        marker_ref = "marker:" + op_id
        observed_revision = max(self._store_revision(uid), 1)
        # COMMITTED receipts ALWAYS observe store_revision >= 1,
        # including no-op commits (sec 3, sec 8)
        self._write_operation_record(uid, lid, request, binding_hash,
                                     marker_ref, outcome, new_epoch,
                                     observed_revision)
        # operation_receipts: one per COMMITTED op
        scope = env.ModelOnlyOperationScope(uid=uid, lifetime=lid,
                                            operation_id=op_id)
        receipt = Receipt(scope=scope, binding_hash=binding_hash,
                          operation_id=op_id,
                          owner=Lifetime(uid=uid, lifetime_id=lid),
                          fence=1, store_revision=observed_revision,
                          outcome="committed", operation_outcome=outcome)
        self._q.q("INSERT INTO operation_receipts(op_id, uid, lifetime_id,"
                  " receipt_json, ref, captured_at)"
                  " VALUES(%s,%s,%s,%s,%s,%s)",
                  (op_id, uid, lid,
                   _canon(receipt.to_dict()).decode("utf-8"),
                   "receipt:" + op_id, now), "exec")
        # stored_requests: the IMMUTABLE original typed FactRequest + binding
        self._q.q("INSERT INTO stored_requests(uid, lifetime_id,"
                  " operation_id, request_json, binding_json, binding_hash,"
                  " captured_at, scrubbed, scrub_revision)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,0,1)",
                  (uid, lid, op_id, request_canonical_json(request),
                   _canon(binding.projection_dict()).decode("utf-8"),
                   binding_hash, now), "exec")
        # READBACK (same step): in-transaction verify; the post-commit
        # re-read is modeled in execute()'s caller via PostReadResult
        current = self._current_view(uid, lid, outcome)
        post = PostReadResult(original_outcome=outcome,
                              applied_at_revision=outcome.applied_at_revision,
                              current_revision=observed_revision,
                              current=current)
        return ExecuteResult(
            kind="executed",
            result=FactResult(owner=Lifetime(uid=uid, lifetime_id=lid),
                              operation_id=op_id, outcome=outcome,
                              receipt=receipt),
            post=post)

    def _record_only_outcome(self, request, plan) -> OperationOutcome:
        if request.method is MethodCode.PUT:
            return OperationOutcome(
                method=MethodCode.PUT, result_code=plan["kind"],
                conflict_fields=plan.get("conflict_fields", ()))
        if request.method is MethodCode.SUPERSEDE:
            return OperationOutcome(
                method=MethodCode.SUPERSEDE, result_code="conflict",
                collided_fid=plan["collided_fid"])
        if request.method is MethodCode.DELETE:
            return OperationOutcome(method=MethodCode.DELETE,
                                    result_code="absent")
        raise cap.ModelInputError()

    def _apply_put(self, uid, lid, request, now):
        fact = request.payload.fact
        fid = self._alloc_fact_id(uid, lid)
        epoch = self._current_epoch(uid)
        norm = _normalize_key(fact.key)
        self._q.q("INSERT INTO facts(uid, lifetime_id, fact_id, key, value,"
                  " category, source, excerpt, captured_at, updated_at,"
                  " row_version, generation, created_op, legacy_sensitive)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,0)",
                  (uid, lid, fid, norm, fact.value, fact.category.value,
                   fact.source.value, fact.provenance.excerpt,
                   fact.provenance.captured_at, now, epoch,
                   request.operation_id), "exec")
        self._q.q("INSERT INTO fact_lineage(uid, lifetime_id, fact_id,"
                  " created_op, last_op, updated_at)"
                  " VALUES(%s,%s,%s,%s,%s,%s)",
                  (uid, lid, fid, request.operation_id,
                   request.operation_id, now), "exec")
        self._q.q("INSERT INTO lineage_ops(uid, lifetime_id, fact_id, seq,"
                  " op_id) VALUES(%s,%s,%s,1,%s)",
                  (uid, lid, fid, request.operation_id), "exec")
        revision = self._bump_revision(uid)
        readback = FactReadback(
            fact_id=fid, key=norm, value=fact.value,
            category=fact.category, source=fact.source,
            provenance=fact.provenance, updated_at=now, row_version=1)
        return OperationOutcome(method=MethodCode.PUT, result_code="applied",
                                applied_at_revision=revision,
                                readback=readback), revision

    def _apply_supersede(self, uid, lid, request, now, binding_hash):
        payload = request.payload
        epoch = self._current_epoch(uid)
        new_fid = self._alloc_fact_id(uid, lid)
        for old_fid in payload.old_fids:
            old = self._q.q("SELECT * FROM facts WHERE uid=%s AND"
                            " lifetime_id=%s AND fact_id=%s",
                            (uid, lid, old_fid), "one")
            if old is None:
                raise FactConflict("CAS")
            history_id = self._alloc_history_id(uid, lid)
            self._q.q(
                "INSERT INTO history(uid, lifetime_id, history_id, fact_id,"
                " source_version, key, value, category, source, excerpt,"
                " captured_at, replacement_fact_id,"
                " replacement_post_commit_version, reason, operation_id,"
                " created_op, binding_hash, superseded_at_epoch)"
                " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
                " %s,%s)",
                (uid, lid, history_id, old_fid, old["row_version"],
                 old["key"], old["value"], old["category"], old["source"],
                 old["excerpt"], old["captured_at"], new_fid, 1,
                 HistoryReason.SUPERSEDE.value, request.operation_id,
                 old["created_op"], binding_hash, epoch), "exec")
            self._q.q("DELETE FROM facts WHERE uid=%s AND lifetime_id=%s"
                      " AND fact_id=%s", (uid, lid, old_fid), "exec")
            self._q.q("UPDATE fact_lineage SET last_op=%s, updated_at=%s"
                      " WHERE uid=%s AND lifetime_id=%s AND fact_id=%s",
                      (request.operation_id, now, uid, lid, old_fid), "exec")
            seq = self._q.q("SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM"
                            " lineage_ops WHERE uid=%s AND lifetime_id=%s"
                            " AND fact_id=%s", (uid, lid, old_fid),
                            "one")["next_seq"]
            self._q.q("INSERT INTO lineage_ops(uid, lifetime_id, fact_id,"
                      " seq, op_id) VALUES(%s,%s,%s,%s,%s)",
                      (uid, lid, old_fid, seq, request.operation_id), "exec")
        fact = payload.replacement
        norm = _normalize_key(fact.key)
        self._q.q("INSERT INTO facts(uid, lifetime_id, fact_id, key, value,"
                  " category, source, excerpt, captured_at, updated_at,"
                  " row_version, generation, created_op, legacy_sensitive)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,0)",
                  (uid, lid, new_fid, norm, fact.value, fact.category.value,
                   fact.source.value, fact.provenance.excerpt,
                   fact.provenance.captured_at, now, epoch,
                   request.operation_id), "exec")
        self._q.q("INSERT INTO fact_lineage(uid, lifetime_id, fact_id,"
                  " created_op, last_op, updated_at)"
                  " VALUES(%s,%s,%s,%s,%s,%s)",
                  (uid, lid, new_fid, request.operation_id,
                   request.operation_id, now), "exec")
        self._q.q("INSERT INTO lineage_ops(uid, lifetime_id, fact_id, seq,"
                  " op_id) VALUES(%s,%s,%s,1,%s)",
                  (uid, lid, new_fid, request.operation_id), "exec")
        revision = self._bump_revision(uid)
        readback = FactReadback(
            fact_id=new_fid, key=norm, value=fact.value,
            category=fact.category, source=fact.source,
            provenance=fact.provenance, updated_at=now, row_version=1)
        return OperationOutcome(method=MethodCode.SUPERSEDE,
                                result_code="applied",
                                applied_at_revision=revision,
                                readback=readback,
                                superseded=tuple(payload.old_fids)), revision

    def _apply_set_optin(self, uid, lid, request, now):
        payload = request.payload
        category = payload.setting.category
        row = self._q.q("SELECT * FROM optins WHERE uid=%s AND"
                        " lifetime_id=%s AND category=%s",
                        (uid, lid, category.value), "one")
        if row is None and payload.value is False:
            # FIRST-False: virtual default-OFF stands; NO row, revision +0
            return OperationOutcome(method=MethodCode.SET_OPTIN,
                                    result_code="unchanged",
                                    record=None), self._store_revision(uid)
        if row is not None and bool(row["value"]) == payload.value:
            record = self._optin_from_row(uid, lid, row)
            return OperationOutcome(method=MethodCode.SET_OPTIN,
                                    result_code="unchanged",
                                    record=record), self._store_revision(uid)
        revision = self._bump_revision(uid)
        policy_revision = self._current_policy_revision(uid)
        prior_version = row["row_version"] if row is not None else 0
        self._q.q("INSERT INTO optins(uid, lifetime_id, category, value,"
                  " actor, policy_revision, operation_id,"
                  " applied_at_revision, updated_at, row_version)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(uid,"
                  " lifetime_id, category) DO UPDATE SET value=%s,"
                  " actor=%s, policy_revision=%s, operation_id=%s,"
                  " applied_at_revision=%s, updated_at=%s, row_version=%s",
                  (uid, lid, category.value, 1 if payload.value else 0,
                   Actor.OWNER.value, policy_revision, request.operation_id,
                   revision, now, prior_version + 1,
                   1 if payload.value else 0, Actor.OWNER.value,
                   policy_revision, request.operation_id, revision, now,
                   prior_version + 1), "exec")
        record = OptinRecord(
            owner=Lifetime(uid=uid, lifetime_id=lid), category=category,
            value=payload.value, actor=Actor.OWNER,
            policy_revision=policy_revision,
            operation_id=request.operation_id,
            applied_at_revision=revision, updated_at=now)
        return OperationOutcome(method=MethodCode.SET_OPTIN,
                                result_code="applied",
                                applied_at_revision=revision,
                                record=record), revision

    def _current_policy_revision(self, uid) -> int:
        row = self._q.q("SELECT policy_revision FROM fixture_snapshot"
                        " WHERE uid=%s", (uid,), "one")
        return int(row["policy_revision"]) if row is not None else 0

    def _optin_from_row(self, uid, lid, row) -> OptinRecord:
        return OptinRecord(
            owner=Lifetime(uid=uid, lifetime_id=lid),
            category=Category(row["category"]),
            value=bool(row["value"]), actor=Actor(row["actor"]),
            policy_revision=row["policy_revision"],
            operation_id=row["operation_id"],
            applied_at_revision=row["applied_at_revision"],
            updated_at=row["updated_at"])

    def _write_operation_record(self, uid, lid, request, binding_hash,
                                marker_ref, outcome, new_epoch,
                                observed_revision) -> None:
        proof_json = None
        if outcome.proof is not None:
            proof_json = _canon(outcome.proof.to_dict()).decode("utf-8")
        self._q.q(
            "INSERT INTO operation_records(uid, lifetime_id, operation_id,"
            " binding_hash, marker, method, result_code, outcome_json,"
            " outcome_kind, proof_json, applied_at_revision, epoch,"
            " store_revision, fence, scrubbed, scrub_revision)"
            " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'full',%s,%s,%s,%s,1,0,1)",
            (uid, lid, request.operation_id, binding_hash, marker_ref,
             request.method.value, outcome.result_code,
             _canon(outcome.to_dict()).decode("utf-8"), proof_json,
             outcome.applied_at_revision, new_epoch, observed_revision),
            "exec")

    def _current_view(self, uid, lid, outcome) -> CurrentResult:
        """The step-9 readback view from the CURRENT rows."""
        if outcome.method is MethodCode.PUT:
            if outcome.result_code == "applied":
                return CurrentResult(kind="available", view=CurrentStateView(
                    view="put", state="live", row=outcome.readback))
            return CurrentResult(kind="available", view=CurrentStateView(
                view="put", state="no_op",
                conflict_fields=outcome.conflict_fields))
        if outcome.method is MethodCode.SUPERSEDE:
            if outcome.result_code == "applied":
                return CurrentResult(kind="available", view=CurrentStateView(
                    view="supersede", state="live", row=outcome.readback,
                    erased=outcome.superseded))
            return CurrentResult(kind="available", view=CurrentStateView(
                view="supersede", state="collision",
                collided_fid=outcome.collided_fid))
        if outcome.method is MethodCode.DELETE:
            state = "erased" if outcome.result_code == "applied" else "absent"
            return CurrentResult(kind="available", view=CurrentStateView(
                view="delete", state=state))
        if outcome.method is MethodCode.WIPE:
            return CurrentResult(kind="available", view=CurrentStateView(
                view="wipe", state="erased",
                safe_report=scrub_operation_outcome(outcome).safe_report))
        return CurrentResult(kind="available", view=CurrentStateView(
            view="optin", state="stored",
            optin=OptInView(kind="stored", record=outcome.record)
            if outcome.record is not None
            else OptInView(kind="default_off")))


FactStore._tombstone_check = _Effects._tombstone_check
FactStore._cas_check = _Effects._cas_check
FactStore._classify = _Effects._classify
FactStore._alloc_fact_id = _Effects._alloc_fact_id
FactStore._alloc_history_id = _Effects._alloc_history_id
FactStore._apply_and_record = _Effects._apply_and_record
FactStore._record_only_outcome = _Effects._record_only_outcome
FactStore._apply_put = _Effects._apply_put
FactStore._apply_supersede = _Effects._apply_supersede
FactStore._apply_set_optin = _Effects._apply_set_optin
FactStore._current_policy_revision = _Effects._current_policy_revision
FactStore._optin_from_row = _Effects._optin_from_row
FactStore._write_operation_record = _Effects._write_operation_record
FactStore._current_view = _Effects._current_view


# ---------------------------------------------------------------------------
# THE ONE ERASE MODEL (sec 11) + closure manifest (sec 13) + reports
# ---------------------------------------------------------------------------

def _closure_entries_for(store, uid, lid, fid) -> list:
    """MEMBERSHIP = every known derived-copy id of the fid (live AND
    history-only AND all-copy) - the ONE DISJOINT ClosureEntry SUM."""
    q = store._q
    entries = []
    live = q.q("SELECT row_version, created_op FROM facts WHERE uid=%s AND"
               " lifetime_id=%s AND fact_id=%s", (uid, lid, fid), "one")
    if live is not None:
        entries.append(ClosureEntry(
            class_name="facts", entry_id=str(fid), state="live",
            version=live["row_version"], op_id=live["created_op"],
            receipt_ref="receipt:" + live["created_op"]))
    for row in q.q("SELECT history_id, source_version, operation_id FROM"
                   " history WHERE uid=%s AND lifetime_id=%s AND fact_id=%s"
                   " ORDER BY history_id", (uid, lid, fid), "all"):
        entries.append(ClosureEntry(
            class_name="history", entry_id=str(row["history_id"]),
            state="history-only", version=row["source_version"],
            op_id=row["operation_id"],
            receipt_ref="receipt:" + row["operation_id"]))
    ops = q.q("SELECT DISTINCT op_id FROM (SELECT op_id FROM lineage_ops"
              " WHERE uid=%s AND lifetime_id=%s AND fact_id=%s UNION"
              " SELECT created_op AS op_id FROM facts WHERE uid=%s AND"
              " lifetime_id=%s AND fact_id=%s UNION SELECT operation_id"
              " AS op_id FROM history WHERE uid=%s AND lifetime_id=%s"
              " AND fact_id=%s)", (uid, lid, fid, uid, lid, fid,
                                  uid, lid, fid), "all")
    for op_row in ops:
        op_id = op_row["op_id"]
        record = q.q("SELECT scrubbed, scrub_revision FROM"
                     " operation_records WHERE uid=%s AND lifetime_id=%s"
                     " AND operation_id=%s", (uid, lid, op_id), "one")
        if record is not None:
            entries.append(ClosureEntry(
                class_name="op_records", entry_id=op_id, op_form="record",
                state=("live" if record["scrubbed"] == 0 else "scrubbed"),
                version=record["scrub_revision"],
                receipt_ref="receipt:" + op_id))
            receipt = q.q("SELECT ref FROM operation_receipts WHERE"
                          " op_id=%s", (op_id,), "one")
            if receipt is not None:
                entries.append(ClosureEntry(
                    class_name="op_records", entry_id=receipt["ref"],
                    op_form="receipt",
                    state=("live" if record["scrubbed"] == 0
                           else "scrubbed"),
                    version=record["scrub_revision"]))
        stored = q.q("SELECT scrubbed, scrub_revision FROM stored_requests"
                     " WHERE uid=%s AND lifetime_id=%s AND operation_id=%s",
                     (uid, lid, op_id), "one")
        if stored is not None:
            entries.append(ClosureEntry(
                class_name="stored_requests", entry_id=op_id,
                state=("live" if stored["scrubbed"] == 0 else "scrubbed"),
                version=stored["scrub_revision"]))
    for copy_row in q.q("SELECT copy_id, kind, version, scrubbed_at FROM"
                        " copy_inventory WHERE uid=%s AND lifetime_id=%s"
                        " AND lineage=%s",
                        (uid, lid, "fact:" + str(fid)), "all"):
        entries.append(ClosureEntry(
            class_name=copy_row["kind"], entry_id=copy_row["copy_id"],
            state=("live" if copy_row["scrubbed_at"] is None
                   else "scrubbed"),
            version=copy_row["version"],
            association={"copy_class": copy_row["kind"],
                         "entry_id": copy_row["copy_id"]}))
    return entries


def _compute_closure_manifest_impl(store, uid, lid, fid,
                                   live_version) -> ExpectedClosureManifest:
    q = store._q
    ops_rows = [(r["fact_id"], r["lifetime_id"], r["op_id"], r["seq"],
                 r["uid"]) for r in q.q(
        "SELECT * FROM lineage_ops WHERE uid=%s AND lifetime_id=%s AND"
        " fact_id=%s", (uid, lid, fid), "all")]
    ops_rows += [(r["fact_id"], r["lifetime_id"], r["op_id"], r["seq"],
                  r["uid"]) for r in q.q(
        "SELECT * FROM erasure_lineage_ops WHERE uid=%s AND lifetime_id=%s"
        " AND fact_id=%s", (uid, lid, fid), "all")]
    copies_rows = [(r["copy_id"], r["fact_id"], r["lifetime_id"], r["uid"])
                   for r in q.q(
        "SELECT * FROM lineage_copies WHERE uid=%s AND lifetime_id=%s AND"
        " fact_id=%s", (uid, lid, fid), "all")]
    copies_rows += [(r["copy_id"], r["fact_id"], r["lifetime_id"], r["uid"])
                    for r in q.q(
        "SELECT * FROM erasure_lineage_copies WHERE uid=%s AND"
        " lifetime_id=%s AND fact_id=%s", (uid, lid, fid), "all")]
    entries = tuple(sorted(
        _closure_entries_for(store, uid, lid, fid),
        key=lambda e: e.identity(uid, lid)))
    return ExpectedClosureManifest(
        owner=Lifetime(uid=uid, lifetime_id=lid), fid=fid,
        live=live_version, entries=entries,
        ops_digest=lineage_ops_digest(ops_rows),
        copies_digest=lineage_copies_digest(copies_rows))


FactStore._compute_closure_manifest = _compute_closure_manifest_impl


class _Erasure:
    """Mixin: the delete/wipe erase engine + report builder."""

    def _apply_delete(self, uid, lid, request, now, binding_hash,
                      new_epoch):
        """delete 'applied': fresh epoch + DeleteProof. THE ONE ERASE MODEL:
        content rows PHYSICALLY REMOVED; identity moves to
        erasure_identities; shared op rows scrub WHOLE-SAFE at first delete."""
        payload = request.payload
        fid = payload.fid
        q = self._q
        scrubbed_fids = [fid]
        counts = _empty_class_counts()

        def bump(cls, removed=0, scrubbed=0):
            entry = counts.setdefault(cls, {"physically_removed": 0,
                                            "content_scrubbed": 0})
            entry["physically_removed"] += removed
            entry["content_scrubbed"] += scrubbed

        # content rows: facts + history physically removed
        live = q.q("SELECT * FROM facts WHERE uid=%s AND lifetime_id=%s"
                   " AND fact_id=%s", (uid, lid, fid), "one")
        erased_keys = set()
        if live is not None:
            erased_keys.add(live["key"])
            q.q("DELETE FROM facts WHERE uid=%s AND lifetime_id=%s AND"
                " fact_id=%s", (uid, lid, fid), "exec")
            bump(CopyClass.FACTS, removed=1)
        history_rows = q.q("SELECT * FROM history WHERE uid=%s AND"
                           " lifetime_id=%s AND fact_id=%s",
                           (uid, lid, fid), "all")
        for row in history_rows:
            erased_keys.add(row["key"])
            q.q("DELETE FROM history WHERE uid=%s AND lifetime_id=%s AND"
                " history_id=%s", (uid, lid, row["history_id"]), "exec")
            bump(CopyClass.HISTORY, removed=1)
        # tombstone markers: future puts of an erased key refuse ERASED
        for norm_key in erased_keys:
            _write_tombstone(q, uid, lid, norm_key, new_epoch, now)
        # identity moves to erasure_identities (ids/commitments ONLY)
        for cls, entry_id in ([(CopyClass.FACTS.value, str(fid))] +
                              [(CopyClass.HISTORY.value,
                                str(r["history_id"]))
                               for r in history_rows]):
            lineage = q.q("SELECT created_op, last_op FROM fact_lineage"
                          " WHERE uid=%s AND lifetime_id=%s AND fact_id=%s",
                          (uid, lid, fid), "one")
            q.q("INSERT OR REPLACE INTO erasure_identities(uid,"
                " lifetime_id, class_name, entry_id, operation_id,"
                " binding_hash, generation, created_op, last_op,"
                " scrubbed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (uid, lid, cls, entry_id, request.operation_id,
                 binding_hash, new_epoch,
                 lineage["created_op"] if lineage else request.operation_id,
                 request.operation_id, now), "exec")
        # lineage MOVES to the erasure tables (FULL membership retained)
        for row in q.q("SELECT * FROM lineage_ops WHERE uid=%s AND"
                       " lifetime_id=%s AND fact_id=%s",
                       (uid, lid, fid), "all"):
            q.q("INSERT INTO erasure_lineage_ops(uid, lifetime_id, fact_id,"
                " seq, op_id) VALUES(%s,%s,%s,%s,%s)",
                (uid, lid, fid, row["seq"], row["op_id"]), "exec")
        q.q("DELETE FROM lineage_ops WHERE uid=%s AND lifetime_id=%s AND"
            " fact_id=%s", (uid, lid, fid), "exec")
        for row in q.q("SELECT * FROM lineage_copies WHERE uid=%s AND"
                       " lifetime_id=%s AND fact_id=%s",
                       (uid, lid, fid), "all"):
            q.q("INSERT INTO erasure_lineage_copies(uid, lifetime_id,"
                " fact_id, copy_id) VALUES(%s,%s,%s,%s)",
                (uid, lid, fid, row["copy_id"]), "exec")
        q.q("DELETE FROM lineage_copies WHERE uid=%s AND lifetime_id=%s"
            " AND fact_id=%s", (uid, lid, fid), "exec")
        q.q("DELETE FROM fact_lineage WHERE uid=%s AND lifetime_id=%s AND"
            " fact_id=%s", (uid, lid, fid), "exec")
        # copy_content rows physically removed; copy_inventory scrubbed
        for copy_row in q.q("SELECT * FROM copy_inventory WHERE uid=%s AND"
                            " lifetime_id=%s AND lineage=%s",
                            (uid, lid, "fact:" + str(fid)), "all"):
            q.q("DELETE FROM copy_content WHERE content_ref=%s",
                (copy_row["content_ref"],), "exec")
            bump(CopyClass.COPY_CONTENT, removed=1)
            q.q("UPDATE copy_inventory SET content_ref='erased',"
                " scrubbed_at=%s WHERE copy_id=%s",
                (now, copy_row["copy_id"]), "exec")
            bump(CopyClass(copy_row["kind"]), scrubbed=1)
        # LINEAGE-DRIVEN SHARED SCRUB: the fid's ops scrub WHOLE-SAFE at
        # THIS delete (first delete of any member fid fires it)
        shared_ops = [r["op_id"] for r in q.q(
            "SELECT DISTINCT op_id FROM erasure_lineage_ops WHERE uid=%s"
            " AND lifetime_id=%s AND fact_id=%s", (uid, lid, fid), "all")]
        if live is not None:
            shared_ops.append(live["created_op"])
        new_rows = 0
        for op_id in dict.fromkeys(shared_ops):
            new_rows += self._scrub_shared_op(uid, lid, op_id, now, bump)
        # the erasure op's OWN new rows: op record + receipt + stored
        # request + report manifest + pages + erasure_identities
        new_rows += 3  # op record + receipt + stored request (step 9's)
        new_rows += len(counts) and (1 + len(counts)) or 2
        revision = self._bump_revision(uid)
        report_ref = "report:delete:" + request.operation_id
        proof = DeleteProof(closure_counts=counts, new_retained=new_rows,
                            scrubbed_fids=tuple(sorted(scrubbed_fids)),
                            report_ref=report_ref)
        outcome = OperationOutcome(method=MethodCode.DELETE,
                                   result_code="applied",
                                   applied_at_revision=revision,
                                   new_epoch=new_epoch, proof=proof)
        self._write_erase_report(uid, lid, request.operation_id, revision,
                                 new_epoch, counts, report_ref, now)
        return outcome, revision

    def _scrub_shared_op(self, uid, lid, op_id, now, bump) -> int:
        """WHOLE-SAFE shared scrub (sec 5): request_json AND binding_json ->
        "erased" under the flag; outcome_json -> structured ScrubbedOutcome;
        receipt_json -> the ONE Receipt with ScrubbedOutcome; proof_json ->
        the SAFE proof. Already-safe rows are re-verified, never re-scrubbed.
        Returns the count of NEW retained rows (0 for scrubs)."""
        q = self._q
        stored = q.q("SELECT scrubbed, scrub_revision FROM stored_requests"
                     " WHERE uid=%s AND lifetime_id=%s AND operation_id=%s",
                     (uid, lid, op_id), "one")
        if stored is not None and stored["scrubbed"] == 0:
            q.q("UPDATE stored_requests SET request_json=%s,"
                " binding_json=%s, scrubbed=1,"
                " scrub_revision=scrub_revision+1 WHERE uid=%s AND"
                " lifetime_id=%s AND operation_id=%s",
                (_ERASED_JSON, _ERASED_JSON, uid, lid, op_id), "exec")
            bump(CopyClass.STORED_REQUESTS, scrubbed=1)
        record = q.q("SELECT * FROM operation_records WHERE uid=%s AND"
                     " lifetime_id=%s AND operation_id=%s",
                     (uid, lid, op_id), "one")
        if record is not None and record["scrubbed"] == 0 \
                and record["outcome_json"] is not None:
            outcome = outcome_from_dict(json.loads(record["outcome_json"]))
            scrubbed = scrub_operation_outcome(outcome)
            q.q("UPDATE operation_records SET outcome_json=%s,"
                " outcome_kind='scrubbed', scrubbed=1,"
                " scrub_revision=scrub_revision+1 WHERE uid=%s AND"
                " lifetime_id=%s AND operation_id=%s",
                (_canon(scrubbed.to_dict()).decode("utf-8"),
                 uid, lid, op_id), "exec")
            bump(CopyClass.OP_RECORDS, scrubbed=1)
            receipt_row = q.q("SELECT * FROM operation_receipts WHERE"
                              " op_id=%s", (op_id,), "one")
            if receipt_row is not None:
                receipt = receipt_from_dict(json.loads(
                    receipt_row["receipt_json"]))
                new_receipt = Receipt(
                    scope=receipt.scope, binding_hash=receipt.binding_hash,
                    operation_id=receipt.operation_id, owner=receipt.owner,
                    fence=receipt.fence,
                    store_revision=receipt.store_revision,
                    outcome="committed", operation_outcome=scrubbed)
                q.q("UPDATE operation_receipts SET receipt_json=%s WHERE"
                    " op_id=%s",
                    (_canon(new_receipt.to_dict()).decode("utf-8"),
                     op_id), "exec")
                bump(CopyClass.OP_RECORDS, scrubbed=1)
        return 0

    def _apply_wipe(self, uid, lid, request, now, new_epoch):
        """wipe 'applied' (incl. empty): erases the owner's ENTIRE inventory
        atomically; NEVER bounded by a request limit. Markers RETAINED."""
        q = self._q
        counts = _empty_class_counts()

        def bump(cls, removed=0, scrubbed=0):
            entry = counts.setdefault(cls, {"physically_removed": 0,
                                            "content_scrubbed": 0})
            entry["physically_removed"] += removed
            entry["content_scrubbed"] += scrubbed

        inventory: list = []  # (class, entry_id) affected ids, owner-scoped
        live_rows = q.q("SELECT * FROM facts WHERE uid=%s AND"
                        " lifetime_id=%s", (uid, lid), "all")
        for _row in live_rows:
            _write_tombstone(q, uid, lid, _row["key"], new_epoch, now)
        history_rows = q.q("SELECT * FROM history WHERE uid=%s AND"
                           " lifetime_id=%s", (uid, lid), "all")
        for _row in history_rows:
            _write_tombstone(q, uid, lid, _row["key"], new_epoch, now)
        optin_rows = q.q("SELECT * FROM optins WHERE uid=%s AND"
                         " lifetime_id=%s", (uid, lid), "all")
        cursor_rows = q.q("SELECT * FROM cursor_registry WHERE uid=%s AND"
                          " lifetime_id=%s", (uid, lid), "all")
        report_page_rows = q.q("SELECT * FROM report_pages WHERE uid=%s AND"
                               " lifetime_id=%s AND status='active'",
                               (uid, lid), "all")
        copy_rows = q.q("SELECT * FROM copy_inventory WHERE uid=%s AND"
                        " lifetime_id=%s AND scrubbed_at IS NULL",
                        (uid, lid), "all")
        op_rows = q.q("SELECT * FROM operation_records WHERE uid=%s AND"
                      " lifetime_id=%s AND scrubbed=0 AND outcome_json IS"
                      " NOT NULL", (uid, lid), "all")
        request_rows = q.q("SELECT * FROM stored_requests WHERE uid=%s AND"
                           " lifetime_id=%s AND scrubbed=0", (uid, lid),
                           "all")
        # physical removals
        q.q("DELETE FROM facts WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        q.q("DELETE FROM history WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        q.q("DELETE FROM optins WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        q.q("DELETE FROM cursor_registry WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        q.q("DELETE FROM fact_lineage WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        for row in live_rows:
            bump(CopyClass.FACTS, removed=1)
            inventory.append((CopyClass.FACTS, str(row["fact_id"])))
        for row in history_rows:
            bump(CopyClass.HISTORY, removed=1)
            inventory.append((CopyClass.HISTORY, str(row["history_id"])))
        for row in optin_rows:
            bump(CopyClass.OPT_INS, removed=1)
            inventory.append((CopyClass.OPT_INS, row["category"]))
        for row in cursor_rows:
            bump(CopyClass.CURSOR_REGISTRY, removed=1)
            inventory.append((CopyClass.CURSOR_REGISTRY, row["cursor_id"]))
        # erasure identity rows for every physically removed fact/history
        for cls, entry_id, gen in (
                [("facts", str(r["fact_id"]), r["generation"])
                 for r in live_rows] +
                [("history", str(r["history_id"]), r["superseded_at_epoch"])
                 for r in history_rows]):
            q.q("INSERT OR REPLACE INTO erasure_identities(uid,"
                " lifetime_id, class_name, entry_id, operation_id,"
                " binding_hash, generation, created_op, last_op,"
                " scrubbed_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (uid, lid, cls, entry_id, request.operation_id,
                 "0" * 64, gen, request.operation_id,
                 request.operation_id, now), "exec")
        # lineage MOVES
        for row in q.q("SELECT * FROM lineage_ops WHERE uid=%s AND"
                       " lifetime_id=%s", (uid, lid), "all"):
            q.q("INSERT INTO erasure_lineage_ops(uid, lifetime_id, fact_id,"
                " seq, op_id) VALUES(%s,%s,%s,%s,%s)",
                (uid, lid, row["fact_id"], row["seq"], row["op_id"]),
                "exec")
        q.q("DELETE FROM lineage_ops WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        for row in q.q("SELECT * FROM lineage_copies WHERE uid=%s AND"
                       " lifetime_id=%s", (uid, lid), "all"):
            q.q("INSERT INTO erasure_lineage_copies(uid, lifetime_id,"
                " fact_id, copy_id) VALUES(%s,%s,%s,%s)",
                (uid, lid, row["fact_id"], row["copy_id"]), "exec")
        q.q("DELETE FROM lineage_copies WHERE uid=%s AND lifetime_id=%s",
            (uid, lid), "exec")
        # content scrubs
        for row in copy_rows:
            q.q("DELETE FROM copy_content WHERE content_ref=%s",
                (row["content_ref"],), "exec")
            bump(CopyClass.COPY_CONTENT, removed=1)
            inventory.append((CopyClass.COPY_CONTENT, row["content_ref"]))
            q.q("UPDATE copy_inventory SET content_ref='erased',"
                " scrubbed_at=%s WHERE copy_id=%s",
                (now, row["copy_id"]), "exec")
            bump(CopyClass(row["kind"]), scrubbed=1)
            inventory.append((CopyClass(row["kind"]), row["copy_id"]))
        for row in report_page_rows:
            q.q("UPDATE report_pages SET ids_json=NULL, status='erased'"
                " WHERE report_id=%s AND page_index=%s",
                (row["report_id"], row["page_index"]), "exec")
            bump(CopyClass.REPORT_PAGES, scrubbed=1)
            inventory.append((CopyClass.REPORT_PAGES,
                              row["report_id"] + ":" +
                              str(row["page_index"])))
        for row in op_rows:
            outcome = outcome_from_dict(json.loads(row["outcome_json"]))
            scrubbed = scrub_operation_outcome(outcome)
            q.q("UPDATE operation_records SET outcome_json=%s,"
                " outcome_kind='scrubbed', scrubbed=1,"
                " scrub_revision=scrub_revision+1 WHERE uid=%s AND"
                " lifetime_id=%s AND operation_id=%s",
                (_canon(scrubbed.to_dict()).decode("utf-8"), uid, lid,
                 row["operation_id"]), "exec")
            bump(CopyClass.OP_RECORDS, scrubbed=1)
            inventory.append((CopyClass.OP_RECORDS, row["operation_id"]))
            receipt_row = q.q("SELECT * FROM operation_receipts WHERE"
                              " op_id=%s", (row["operation_id"],), "one")
            if receipt_row is not None:
                receipt = receipt_from_dict(json.loads(
                    receipt_row["receipt_json"]))
                new_receipt = Receipt(
                    scope=receipt.scope, binding_hash=receipt.binding_hash,
                    operation_id=receipt.operation_id, owner=receipt.owner,
                    fence=receipt.fence,
                    store_revision=receipt.store_revision,
                    outcome="committed", operation_outcome=scrubbed)
                q.q("UPDATE operation_receipts SET receipt_json=%s WHERE"
                    " op_id=%s",
                    (_canon(new_receipt.to_dict()).decode("utf-8"),
                     row["operation_id"]), "exec")
                bump(CopyClass.OP_RECORDS, scrubbed=1)
                inventory.append((CopyClass.OP_RECORDS,
                                  "receipt:" + row["operation_id"]))
        for row in request_rows:
            q.q("UPDATE stored_requests SET request_json=%s,"
                " binding_json=%s, scrubbed=1,"
                " scrub_revision=scrub_revision+1 WHERE uid=%s AND"
                " lifetime_id=%s AND operation_id=%s",
                (_ERASED_JSON, _ERASED_JSON, uid, lid,
                 row["operation_id"]), "exec")
            bump(CopyClass.STORED_REQUESTS, scrubbed=1)
            inventory.append((CopyClass.STORED_REQUESTS,
                              row["operation_id"]))
        # retained whitelist (markers NEVER affected; honest report)
        retained = (
            RetainedClass(name=RetainedClassName.OWNER_COUNTERS,
                          reason="monotone epoch allocator survives"),
            RetainedClass(name=RetainedClassName.STORE_REVISION_COUNTER,
                          reason="monotone revision counter survives"),
            RetainedClass(name=RetainedClassName.LIFETIME,
                          reason="owner/lifetime identity survives"),
            RetainedClass(name=RetainedClassName.MARKER_KEYS,
                          reason="fixture key material retained so"
                                 " existing exclusions keep resolving"),
            RetainedClass(name=RetainedClassName.MARKER_ENTRIES,
                          reason="existing exclusion markers retained"),
            RetainedClass(name=RetainedClassName.OP_RECORD_NONCONTENT,
                          reason="safe commitments/ids retained"),
            RetainedClass(name=RetainedClassName.ACCOUNT_AUTH_FIXTURES,
                          reason="owned fixture tables survive"),
            RetainedClass(name=RetainedClassName.SAFE_EFFECT_METADATA,
                          reason="safe reports/receipts/manifests of the"
                                 " effect itself are retained"),
        )
        revision = self._bump_revision(uid)
        report_id = "report:wipe:" + request.operation_id
        # paged delivery: first page <=4096 ids; pages per class, ids sorted
        pages = self._write_wipe_report_pages(
            uid, lid, request.operation_id, report_id, revision, new_epoch,
            counts, inventory, now)
        new_rows = 3 + 1 + len(pages)  # op record + receipt + stored request
        # + manifest + page rows
        first = pages[0]
        report = WipeReport(
            owner=Lifetime(uid=uid, lifetime_id=lid),
            operation_id=request.operation_id, new_epoch=new_epoch,
            report_id=report_id, page_count=len(pages), counts=counts,
            new_retained=new_rows, commit_revision=revision,
            scope=WipeScope.OWNER_MEMORY_FULL, first_page=first,
            retained=retained,
            unknown_coverage=(UnknownCoverage.PROVIDER_SIDE,),
            external_revocations=(ExternalRevocation.ATTEMPTED_UNKNOWN,),
            future_derivation_state=
            FutureDerivationState.EXCLUSIONS_RETAINED)
        outcome = OperationOutcome(method=MethodCode.WIPE,
                                   result_code="applied",
                                   applied_at_revision=revision,
                                   new_epoch=new_epoch, report=report)
        return outcome, revision

    def _write_erase_report(self, uid, lid, op_id, revision, new_epoch,
                            counts, report_ref, now) -> None:
        manifest_json = _canon({
            "class_actions": _class_counts_json(counts),
            "new_retained": 0}).decode("utf-8")
        self._q.q("INSERT OR REPLACE INTO report_manifest(report_id, uid,"
                  " lifetime_id, operation_id, commit_revision, new_epoch,"
                  " class_counts_json, page_count, status)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,1,'active')",
                  (report_ref, uid, lid, op_id, revision, new_epoch,
                   manifest_json), "exec")

    def _write_wipe_report_pages(self, uid, lid, op_id, report_id, revision,
                                 new_epoch, counts, inventory,
                                 now) -> list:
        """Per-CLASS ranges; totals across ALL pages; IDs ascending; the
        4096 bound is a PAGE bound ONLY - the inventory erases COMPLETELY."""
        by_class: dict = {}
        for cls, entry_id in inventory:
            by_class.setdefault(cls, []).append(entry_id)
        pages = []
        page_index = 0
        total_new_retained = 0
        for cls in sorted(by_class.keys(), key=lambda c: c.value):
            ids = sorted(set(by_class[cls]))
            for start in range(0, max(len(ids), 1), PAGE_MAX):
                chunk = ids[start:start + PAGE_MAX]
                refs = tuple(InventoryRef(
                    class_name=cls, entry_id=entry_id,
                    owner=Lifetime(uid=uid, lifetime_id=lid))
                    for entry_id in chunk)
                rng = ReportRange(
                    report_id=report_id,
                    owner=Lifetime(uid=uid, lifetime_id=lid),
                    operation_id=op_id, class_name=cls,
                    commit_revision=revision, page_index=page_index,
                    page_size=max(len(chunk), 1), total_count=len(ids),
                    per_class_totals=counts, new_retained=0, ids=refs)
                pages.append(rng)
                ids_json = _canon([ref.to_dict() for ref in refs]
                                  ).decode("utf-8")
                self._q.q(
                    "INSERT INTO report_pages(report_id, page_index, uid,"
                    " lifetime_id, operation_id, class_name, range_start,"
                    " range_end, commit_revision, ids_json, status)"
                    " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'active')",
                    (report_id, page_index, uid, lid, op_id, cls.value,
                     chunk[0] if chunk else "", chunk[-1] if chunk else "",
                     revision, ids_json), "exec")
                page_index += 1
        if not pages:
            rng = ReportRange(
                report_id=report_id,
                owner=Lifetime(uid=uid, lifetime_id=lid),
                operation_id=op_id, class_name=CopyClass.FACTS,
                commit_revision=revision, page_index=0, page_size=1,
                total_count=0, per_class_totals=counts, new_retained=0,
                ids=())
            pages.append(rng)
            self._q.q(
                "INSERT INTO report_pages(report_id, page_index, uid,"
                " lifetime_id, operation_id, class_name, range_start,"
                " range_end, commit_revision, ids_json, status)"
                " VALUES(%s,0,%s,%s,%s,%s,'','',%s,'[]','active')",
                (report_id, uid, lid, op_id, CopyClass.FACTS.value,
                 revision), "exec")
        manifest_json = _canon({
            "class_actions": _class_counts_json(counts),
            "new_retained": total_new_retained}).decode("utf-8")
        self._q.q("INSERT INTO report_manifest(report_id, uid, lifetime_id,"
                  " operation_id, commit_revision, new_epoch,"
                  " class_counts_json, page_count, status)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'active')",
                  (report_id, uid, lid, op_id, revision, new_epoch,
                   manifest_json, len(pages)), "exec")
        return pages


FactStore._apply_delete = _Erasure._apply_delete
FactStore._scrub_shared_op = _Erasure._scrub_shared_op
FactStore._apply_wipe = _Erasure._apply_wipe
FactStore._write_erase_report = _Erasure._write_erase_report
FactStore._write_wipe_report_pages = _Erasure._write_wipe_report_pages

_ERASED_JSON = json.dumps("erased")


# ---------------------------------------------------------------------------
# READ APIs (sec 13) - THE ONE ORDERING: handle -> projection -> Phase A ->
# historical original-load -> outcome. Reads NEVER write.
# ---------------------------------------------------------------------------

def _read_projection(store, method: ReadMethod, owner_uid: int,
                     lifetime_id: int, targets: dict,
                     clock_reading: int, caller_canon: dict,
                     handle: str) -> str:
    """The READ wrapper commitment (v80 3496-3501, EXACT): SHA-256 over
    the canonical JSON of {session_id, handle, method, owner: {uid,
    lifetime_id}, targets, context: {caller, clock_reading}} - bound to
    the ACTUAL SUPPLIED caller context at call time (a changed
    uid/lifetime/generation/epoch/revision/issuer snapshot NEVER
    compares equal), NEVER a literal receiver string."""
    return _sha256_hex(_canon({
        "context": {"caller": caller_canon, "clock_reading": clock_reading},
        "handle": handle,
        "method": method.value,
        "owner": {"lifetime_id": lifetime_id, "uid": owner_uid},
        "session_id": store._session_id,
        "targets": targets,
    }))


class _Reads:
    """Mixin: the read surface."""

    def _read_prologue(self, owner_uid, capability, method, targets,
                       context):
        """(1) HANDLE validation -> (2) PROJECTION COMPARISON over the
        REGISTERED read context -> BEGIN -> (3) issuer + fresh READ
        Phase A INSIDE the one read transaction (V44). Returns
        (inner capability, lifetime_id); the caller reads and COMMITs
        in THIS transaction."""
        _req_int(owner_uid, 0)
        if type(context) is not env.ModelOnlyCallerContext:
            raise cap.ModelInputError()
        inner = self._validate_handle(capability, SessionBoundCapability,
                                      "capability")
        lid = inner.identity.lifetime_id
        _req_int(lid, 1)
        registered = self._read_contexts.get(capability.handle)
        if registered is None:
            raise cap.AuthError()  # MISSING_IDENTITY - not a read handle
        # ISSUER STALENESS from the caller context data (NO db read -
        # the issuer ROW re-read stays inside the txn per V44):
        # current_issuer_valid False = STALE (sec 4, V55); ORIGINAL
        # issuer == CURRENT issuer - a replaced session/issuer is
        # STALE even with uid/revisions unchanged (R2)
        if not context.current_issuer_valid:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        if context.original_binding.issuer != context.current_issuer:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        # the commitment binds the ACTUAL SUPPLIED caller context
        # (uid/lifetime/generations/epochs/revisions/issuer snapshot)
        # + the handle - NOT the registered copy (v80 3496-3501)
        supplied = json.loads(_canon(_context_canon(context)))
        expected = _read_projection(self, method, owner_uid, lid, targets,
                                    registered["clock_reading"],
                                    supplied, capability.handle)
        if expected != capability.binding_hash:
            raise cap.AuthError()  # MISSING_IDENTITY - changed
            # targets/context/handle
        if identity_mismatch(inner, owner_uid, lid):
            raise cap.AuthError()
        self._read_txn()
        try:
            # CURRENT issuer re-read: TYPED equality + BOTH validity
            # flags (the caller's flag is NOT proof; the table re-read
            # is - sec 4; current_issuer_valid False = STALE, V55)
            issuer_row = self._q.q(
                "SELECT issuer_json, valid FROM issuer_state WHERE uid=%s",
                (owner_uid,), "one")
            if issuer_row is None or not issuer_row["valid"] \
                    or not context.current_issuer_valid:
                raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
            stored_issuer = _typed_issuer_from_json(issuer_row["issuer_json"])
            if stored_issuer is not None \
                    and stored_issuer != context.current_issuer:
                raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
            cap_request = cap.Request(
                operation_id=inner.request.operation_id,
                operation_class=cap.OperationClass.READ,
                required_scopes=frozenset({cap.Scope.MEMORY_READ}),
                captured_epoch=None, requested_audience_id=None,
                permitted_information_ref="read:" + method.value,
                extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
                group_action=None, sensitive=False)
            decision = self._evaluator.recheck_model(inner, cap_request)
            if not decision.allowed:
                raise _phase_a_error(decision.code)
        except Exception:
            self._read_abort()
            raise
        return inner, lid

    def _read_txn(self):
        self._q.raw.execute("BEGIN IMMEDIATE")

    def _read_commit(self):
        self._q.raw.execute("COMMIT")

    def _read_abort(self):
        try:
            self._q.raw.execute("ROLLBACK")
        except Exception:
            pass

    def get(self, owner_uid: int, fid: int,
            capability: SessionBoundCapability, clock: StoreClock,
            context: "env.ModelOnlyCallerContext") -> GetResult:
        _req_int(fid, 1)
        targets = {"fid": fid}
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.GET, targets, context)
        try:
            row = self._q.q("SELECT * FROM facts WHERE uid=%s AND"
                            " lifetime_id=%s AND fact_id=%s",
                            (owner_uid, lid, fid), "one")
            revision = self._store_revision(owner_uid)
            self._read_commit()
        except Exception:
            self._read_abort()
            raise
        return GetResult(owner=Lifetime(uid=owner_uid, lifetime_id=lid),
                         query_revision=max(revision, 1),
                         row=(_fact_row_to_readback(row)
                              if row is not None else None))

    def get_optin(self, owner_uid: int, setting: OptInSetting,
                  capability: SessionBoundCapability, clock: StoreClock,
                  context: "env.ModelOnlyCallerContext") -> GetOptinResult:
        if type(setting) is not OptInSetting:
            raise cap.ModelInputError()
        targets = {"setting": {"category": setting.category.value}}
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.GET_OPTIN, targets, context)
        try:
            row = self._q.q("SELECT * FROM optins WHERE uid=%s AND"
                            " lifetime_id=%s AND category=%s",
                            (owner_uid, lid, setting.category.value), "one")
            revision = self._store_revision(owner_uid)
            self._read_commit()
        except Exception:
            self._read_abort()
            raise
        view = (OptInView(kind="stored",
                          record=self._optin_from_row(owner_uid, lid, row))
                if row is not None else OptInView(kind="default_off"))
        return GetOptinResult(owner=Lifetime(uid=owner_uid,
                                             lifetime_id=lid),
                              query_revision=max(revision, 1), view=view)

    def _cursor_open(self, uid, lid, cursor_id, query_kind):
        """The sec-12 rules: structure -> registry -> invalidation."""
        if (type(cursor_id) is not str or len(cursor_id) != CURSOR_WIRE_LEN
                or any(c not in "0123456789abcdef" for c in cursor_id)):
            raise cap.ModelInputError()
        row = self._q.q("SELECT * FROM cursor_registry WHERE cursor_id=%s"
                        " AND uid=%s AND lifetime_id=%s AND query_kind=%s",
                        (cursor_id, uid, lid, query_kind.value), "one")
        if row is None:
            raise UnknownCursorError()
        if query_kind is QueryKind.FACTS:
            if row["query_revision"] != self._store_revision(uid):
                raise UnknownCursorError()  # live-revision movement
        return row

    def _cursor_mint(self, uid, lid, query_kind, filter_hash, position,
                     row_kind) -> str:
        cursor_id = secrets.token_hex(16)
        self._q.q("INSERT INTO cursor_registry(cursor_id, uid, lifetime_id,"
                  " query_kind, filter_hash, query_revision, position,"
                  " row_kind, issued_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,"
                  "%s)",
                  (cursor_id, uid, lid, query_kind.value, filter_hash,
                   self._store_revision(uid), position, row_kind.value,
                   _clock_now(self._clock)), "exec")
        return cursor_id

    def list_facts(self, owner_uid: int, row_kind: RowKind,
                   category: Optional[Category], cursor: Optional[str],
                   limit: int, capability: SessionBoundCapability,
                   clock: StoreClock,
                   context: "env.ModelOnlyCallerContext") -> FactPage:
        _req_enum(row_kind, RowKind)
        if category is not None:
            _req_enum(category, Category)
        _req_int(limit, LIST_LIMIT_MIN, LIST_LIMIT_MAX)
        if cursor is not None and (
                type(cursor) is not str or len(cursor) != CURSOR_WIRE_LEN
                or any(c not in "0123456789abcdef" for c in cursor)):
            raise cap.ModelInputError()  # malformed cursor: STRUCTURAL
        targets = {"category": category.value if category else None,
                   "cursor": cursor, "limit": limit,
                   "row_kind": row_kind.value}
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.LIST_FACTS, targets, context)
        try:
            position = 0
            if cursor is not None:
                position = self._cursor_open(
                    owner_uid, lid, cursor, QueryKind.FACTS)["position"]
            filter_hash = _sha256_hex(_canon({
                "category": category.value if category else None,
                "row_kind": row_kind.value}))[:FILTER_HASH_LEN]
            if row_kind is RowKind.LIVE:
                sql = ("SELECT * FROM facts WHERE uid=%s AND lifetime_id=%s"
                       + (" AND category=%s" if category else "")
                       + " ORDER BY fact_id LIMIT %s OFFSET %s")
                params = ((owner_uid, lid, category.value, limit + 1,
                           position) if category else
                          (owner_uid, lid, limit + 1, position))
                rows = [_fact_row_to_readback(r)
                        for r in self._q.q(sql, params, "all")]
            else:
                sql = ("SELECT * FROM history WHERE uid=%s AND"
                       " lifetime_id=%s"
                       + (" AND category=%s" if category else "")
                       + " ORDER BY history_id LIMIT %s OFFSET %s")
                params = ((owner_uid, lid, category.value, limit + 1,
                           position) if category else
                          (owner_uid, lid, limit + 1, position))
                rows = [_history_row(r) for r in self._q.q(sql, params,
                                                           "all")]
            next_cursor = None
            if len(rows) > limit:
                rows = rows[:limit]
                next_cursor = self._cursor_mint(owner_uid, lid,
                                                QueryKind.FACTS,
                                                filter_hash,
                                                position + limit, row_kind)
            revision = self._store_revision(owner_uid)
            self._read_commit()
        except Exception:
            self._read_abort()
            raise
        return FactPage(owner=Lifetime(uid=owner_uid, lifetime_id=lid),
                        query_revision=max(revision, 1), row_kind=row_kind,
                        rows=tuple(rows), next_cursor=next_cursor,
                        bounded_by="limit:" + str(limit))

    def count_facts(self, owner_uid: int, filters: CountFilters,
                    capability: SessionBoundCapability, clock: StoreClock,
                    context: "env.ModelOnlyCallerContext") -> CountResult:
        if type(filters) is not CountFilters:
            raise cap.ModelInputError()
        targets = {"filters": {
            "category": (filters.category.value
                         if filters.category else None),
            "include_superseded": filters.include_superseded}}
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.COUNT_FACTS, targets, context)
        try:
            value = 0
            sql = ("SELECT COUNT(*) AS n FROM facts WHERE uid=%s AND"
                   " lifetime_id=%s"
                   + (" AND category=%s" if filters.category else ""))
            params = ((owner_uid, lid, filters.category.value)
                      if filters.category else (owner_uid, lid))
            value += self._q.q(sql, params, "one")["n"]
            if filters.include_superseded:
                sql = ("SELECT COUNT(*) AS n FROM history WHERE uid=%s AND"
                       " lifetime_id=%s"
                       + (" AND category=%s" if filters.category else ""))
                value += self._q.q(sql, params, "one")["n"]
            revision = self._store_revision(owner_uid)
            self._read_commit()
        except Exception:
            self._read_abort()
            raise
        return CountResult(owner=Lifetime(uid=owner_uid, lifetime_id=lid),
                           query_revision=max(revision, 1), filters=filters,
                           value=value)

    def read_report_page(self, owner_uid: int, report_id: str,
                         page_index: int,
                         capability: SessionBoundCapability,
                         clock: StoreClock,
                         context: "env.ModelOnlyCallerContext"):
        _req_str(report_id, BOUNDED_STR_MAX)
        _req_int(page_index, 0)
        targets = {"page_index": page_index, "report_id": report_id}
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.READ_REPORT_PAGE, targets, context)
        try:
            page = self._q.q("SELECT * FROM report_pages WHERE report_id=%s"
                             " AND page_index=%s AND uid=%s AND"
                             " lifetime_id=%s",
                             (report_id, page_index, owner_uid, lid), "one")
            manifest = self._q.q("SELECT * FROM report_manifest WHERE"
                                 " report_id=%s", (report_id,), "one")
            if manifest is not None and manifest["status"] == "erased" or \
                    page is not None and page["status"] == "erased":
                self._read_commit()
                return PageUnavailable(
                    kind="erased", report_id=report_id,
                    owner=Lifetime(uid=owner_uid, lifetime_id=lid),
                    operation_id=(page["operation_id"] if page is not None
                                  else manifest["operation_id"]),
                    commit_revision=(page["commit_revision"]
                                     if page is not None else
                                     manifest["commit_revision"]))
            if page is None or manifest is None:
                self._read_commit()
                return PageUnavailable(kind="unknown")
            ids = None
            if page["ids_json"] is not None:
                ids = tuple(InventoryRef(
                    class_name=CopyClass(ref["class_name"]),
                    entry_id=ref["entry_id"],
                    owner=Lifetime(uid=ref["owner"]["uid"],
                                   lifetime_id=ref["owner"]["lifetime_id"]))
                    for ref in json.loads(page["ids_json"]))
            rng = ReportRange(
                report_id=report_id,
                owner=Lifetime(uid=owner_uid, lifetime_id=lid),
                operation_id=page["operation_id"],
                class_name=CopyClass(page["class_name"]),
                commit_revision=page["commit_revision"],
                page_index=page_index,
                page_size=max(len(ids or ()), 1),
                total_count=len(ids or ()),
                per_class_totals=_class_counts_from_json(
                    json.loads(manifest["class_counts_json"])
                    ["class_actions"]),
                new_retained=json.loads(manifest["class_counts_json"])
                ["new_retained"],
                ids=ids or ())
            self._read_commit()
            return rng
        except Exception:
            self._read_abort()
            raise

    def prepare_closure(self, owner_uid: int, fid: int,
                        capability: SessionBoundCapability,
                        clock: StoreClock,
                        context: "env.ModelOnlyCallerContext"
                        ) -> ExpectedClosureManifest:
        """READ-ONLY preview for CAS preparation (fixture-only in the
        MODEL; the PRODUCTION path is a stub that refuses)."""
        _req_int(fid, 1)
        targets = {"fid": fid}
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.PREPARE_CLOSURE, targets, context)
        try:
            live = self._q.q("SELECT row_version FROM facts WHERE uid=%s"
                             " AND lifetime_id=%s AND fact_id=%s",
                             (owner_uid, lid, fid), "one")
            manifest = self._compute_closure_manifest(
                owner_uid, lid, fid,
                live["row_version"] if live is not None else None)
            self._read_commit()
            return manifest
        except Exception:
            self._read_abort()
            raise


def identity_mismatch(capability, owner_uid, lifetime_id) -> bool:
    return (capability.identity.owner_uid != owner_uid
            or capability.identity.lifetime_id != lifetime_id)


def _history_row(row) -> HistoryRow:
    return HistoryRow(
        history_id=row["history_id"], fact_id=row["fact_id"],
        source_version=row["source_version"], key=row["key"],
        value=row["value"], category=Category(row["category"]),
        source=Source(row["source"]), excerpt=row["excerpt"],
        captured_at=row["captured_at"],
        replacement_fact_id=row["replacement_fact_id"],
        replacement_post_commit_version=
        row["replacement_post_commit_version"],
        reason=HistoryReason(row["reason"]),
        operation_id=row["operation_id"], created_op=row["created_op"],
        binding_hash=row["binding_hash"],
        superseded_at_epoch=row["superseded_at_epoch"])


FactStore._read_prologue = _Reads._read_prologue
FactStore._read_txn = _Reads._read_txn
FactStore._read_commit = _Reads._read_commit
FactStore._read_abort = _Reads._read_abort
FactStore.get = _Reads.get
FactStore.get_optin = _Reads.get_optin
FactStore._cursor_open = _Reads._cursor_open
FactStore._cursor_mint = _Reads._cursor_mint
FactStore.list_facts = _Reads.list_facts
FactStore.count_facts = _Reads.count_facts
FactStore.read_report_page = _Reads.read_report_page
FactStore.prepare_closure = _Reads.prepare_closure


# ---------------------------------------------------------------------------
# Historical reads: readback + resolve (sec 11/13) + evidence insertion
# ---------------------------------------------------------------------------

class _Historical:
    """Mixin: readback, resolve, evidence."""

    def _historical_targets(self, operation_id: str, original_request,
                            original_binding) -> dict:
        """The HistoricalTarget CLOSED SUM (sec 13): SUPPLIED binds the
        RECOMPUTED single extended hash from the caller's typed
        originals; LOAD_STORED binds the op + the TYPED original
        binding. The discriminant IS the shape."""
        _req_hex(operation_id, OP_ID_LEN)
        if original_request is None:
            if type(original_binding) is not env.ModelOnlyRequestBinding:
                raise cap.ModelInputError()
            originals = {
                "mode": "LOAD_STORED", "operation_id": operation_id,
                "original_binding": json.loads(_canon(
                    original_binding.projection_dict()))}
        else:
            if type(original_request) is not FactRequest \
                    or type(original_binding) \
                    is not env.ModelOnlyRequestBinding:
                raise cap.ModelInputError()
            originals = {
                "mode": "SUPPLIED",
                "recomputed_commitment": extended_binding_hash(
                    original_binding, original_request)}
        return {"operation_id": operation_id, "originals": originals}

    def _historical_load(self, owner_uid, lid, operation_id,
                         original_request, original_binding):
        """The scoped authenticated original-load + FULL hash verification
        (sec 13 step 4). Returns (mode, record_row|None, binding_hash)."""
        _req_hex(operation_id, OP_ID_LEN)
        q = self._q
        stored = q.q("SELECT * FROM stored_requests WHERE uid=%s AND"
                     " lifetime_id=%s AND operation_id=%s",
                     (owner_uid, lid, operation_id), "one")
        record = q.q("SELECT * FROM operation_records WHERE uid=%s AND"
                     " lifetime_id=%s AND operation_id=%s",
                     (owner_uid, lid, operation_id), "one")
        blocked = q.q("SELECT * FROM blocked_records WHERE uid=%s AND"
                      " lifetime_id=%s AND operation_id=%s",
                      (owner_uid, lid, operation_id), "one")
        resolved = q.q("SELECT * FROM resolved_accounting WHERE uid=%s AND"
                       " lifetime_id=%s AND operation_id=%s",
                       (owner_uid, lid, operation_id), "one")
        if stored is None and record is None and blocked is None \
                and resolved is None:
            raise UnknownOperationError()
        if original_request is None:
            # LOAD_STORED: PRE-SCRUB ONLY - post-scrub the erased columns
            # CANNOT be parsed
            if stored is None:
                raise UnknownOperationError()
            if stored["scrubbed"] == 1:
                raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
            if type(original_binding) is not env.ModelOnlyRequestBinding:
                raise cap.ModelInputError()
            # the REQUIRED original-binding comparison: the AUTHENTICATED
            # LOAD supplies the retained commitment; the caller's typed
            # original binding is compared EQUAL to the STORED
            # projection BEFORE any outcome decode (sec 13)
            stored_binding = json.loads(stored["binding_json"])
            if original_binding.projection_dict() != stored_binding:
                raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
            # the FULL request+binding hash recomputation (B7): the
            # caller's typed binding is re-hashed WITH THE STORED typed
            # request (the exact extended_binding_hash construction)
            # and MUST equal the retained commitment; a stored row
            # whose request/binding pair no longer hashes to its
            # commitment = drift
            try:
                stored_req = json.loads(stored["request_json"])
                recomputed = _sha256_hex(_canon({
                    "binding": original_binding.projection_dict(),
                    "pr5": {"method": stored_req["method"],
                            "payload": stored_req["payload"]}}))
            except (ValueError, KeyError, TypeError):
                raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
            if recomputed != stored["binding_hash"]:
                raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
            binding_hash = stored["binding_hash"]
            return LoadStoredMode(operation_id=operation_id,
                                  original_binding=original_binding), \
                record, binding_hash
        # SUPPLIED: the caller's TYPED ORIGINALS; the store RECOMPUTES the
        # single extended hash and compares EQUAL
        if type(original_request) is not FactRequest:
            raise cap.ModelInputError()
        if type(original_binding) is not env.ModelOnlyRequestBinding:
            raise cap.ModelInputError()
        recomputed = extended_binding_hash(original_binding, original_request)
        anchor = stored["binding_hash"] if stored is not None else \
            (record["binding_hash"] if record is not None else
             (blocked["binding_hash"] if blocked is not None else
              resolved["commitment"]))
        if recomputed != anchor:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        return SuppliedMode(recomputed_commitment=recomputed), record, \
            recomputed

    def readback(self, owner_uid: int, operation_id: str,
                 original_request: Optional[FactRequest],
                 original_binding: "env.ModelOnlyRequestBinding",
                 capability: SessionBoundCapability, clock: StoreClock,
                 context: "env.ModelOnlyCallerContext"):
        """Historical; writes NO records. PostReadResult ONLY for a known
        committed outcome; BlockedStatus for unresolved."""
        targets = self._historical_targets(operation_id,
                                           original_request,
                                           original_binding)
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.READBACK, targets,
                                         context)
        try:
            _mode, record, binding_hash = self._historical_load(
                owner_uid, lid, operation_id, original_request,
                original_binding)
            if record is None or record["outcome_json"] is None:
                blocked = self._q.q("SELECT * FROM blocked_records WHERE"
                                    " uid=%s AND lifetime_id=%s AND"
                                    " operation_id=%s",
                                    (owner_uid, lid, operation_id), "one")
                self._read_commit()
                if blocked is not None:
                    return BlockedStatus(status="durable_blocked",
                                         record=_blocked_from_row(blocked))
                return BlockedStatus(status="local_uncertain")
            outcome = _decode_stored_outcome(record)
            current = self._historical_current_view(owner_uid, lid, outcome)
            revision = self._store_revision(owner_uid)
            self._read_commit()
            return PostReadResult(
                original_outcome=outcome,
                applied_at_revision=record["applied_at_revision"],
                current_revision=max(revision, 1), current=current)
        except Exception:
            self._read_abort()
            raise

    def _historical_current_view(self, uid, lid, outcome) -> CurrentResult:
        """The CURRENT-state read for a historical outcome."""
        if isinstance(outcome, ScrubbedOutcome):
            if outcome.method is MethodCode.PUT:
                return CurrentResult(kind="available", view=CurrentStateView(
                    view="put", state="erased"))
            if outcome.method is MethodCode.DELETE:
                return CurrentResult(kind="available", view=CurrentStateView(
                    view="delete", state="erased"))
            if outcome.method is MethodCode.WIPE:
                return CurrentResult(kind="available", view=CurrentStateView(
                    view="wipe", state="erased",
                    safe_report=outcome.safe_report))
            if outcome.method is MethodCode.SET_OPTIN:
                return CurrentResult(kind="available", view=CurrentStateView(
                    view="optin", state="stored",
                    optin=OptInView(kind="default_off")))
            return CurrentResult(kind="absent")
        return self._current_view(uid, lid, outcome)

    def resolve(self, owner_uid: int, operation_id: str,
                original_request: Optional[FactRequest],
                original_binding: "env.ModelOnlyRequestBinding",
                capability: SessionBoundCapability, clock: StoreClock,
                context: "env.ModelOnlyCallerContext",
                resolver: ResolverCode = ResolverCode.MANUAL
                ) -> ResolutionResult:
        """Historical; resolver writes are EXACTLY the RESOLVER WRITE
        WHITELIST. EVIDENCE NEVER REOPENS EXECUTION."""
        _req_enum(resolver, ResolverCode)
        targets = self._historical_targets(operation_id,
                                           original_request,
                                           original_binding)
        inner, lid = self._read_prologue(owner_uid, capability,
                                         ReadMethod.RESOLVE, targets,
                                         context)
        now = _clock_now(self._clock)
        try:
            _mode, record, binding_hash = self._historical_load(
                owner_uid, lid, operation_id, original_request,
                original_binding)
            q = self._q
            recovery_id = _recovery_id(owner_uid, lid, operation_id,
                                       binding_hash)
            existing = q.q("SELECT * FROM resolved_accounting WHERE"
                           " recovery_id=%s", (recovery_id,), "one")
            if existing is not None:
                # RESOLUTION LOST-ACK / REPEAT: LOAD the existing
                # accounting - the SAME event id, source, counts; NO writes
                self._read_commit()
                return self._resolution_from_accounting(
                    existing, owner_uid, lid, operation_id, record)
            committed = record is not None and record["marker"] is not None \
                and record["outcome_json"] is not None
            if committed:
                # CRASH-RESOLVE ORDERING: durable committed record FIRST -
                # resolve COMMITTED from the record (a replay, never a
                # rollback)
                outcome = _decode_stored_outcome(record)
                event_id = "resolve:" + operation_id
                self._write_resolution(owner_uid, lid, operation_id,
                                       recovery_id, binding_hash,
                                       "committed", event_id, now,
                                       attested=0)
                current = self._historical_current_view(owner_uid, lid,
                                                        outcome)
                post = PostReadResult(
                    original_outcome=outcome,
                    applied_at_revision=record["applied_at_revision"],
                    current_revision=max(self._store_revision(owner_uid), 1),
                    current=current)
                self._read_commit()
                return ResolutionResult(
                    kind="committed", post=post,
                    resolution_event_id=event_id, resolved_count=1,
                    attested_witness_count=0, new_retained=2)
            # UNCOMMITTED: the SERIALIZED FENCE + attestation, then the
            # rolled_back accounting - NEVER absence-as-proof
            witnesses = q.q("SELECT * FROM abort_witnesses WHERE"
                            " operation_id=%s", (operation_id,), "all")
            armed = [w for w in witnesses if w["state"] == "armed"]
            if not witnesses:
                # an empty witness set is NEVER rollback evidence (sec 7):
                # the operation stays UNRESOLVED, no event, no writes
                self._read_commit()
                blocked_row = q.q("SELECT * FROM blocked_records WHERE"
                                  " uid=%s AND lifetime_id=%s AND"
                                  " operation_id=%s",
                                  (owner_uid, lid, operation_id), "one")
                return BlockedStatus(
                    status="durable_blocked",
                    record=_blocked_from_row(blocked_row)
                    if blocked_row is not None else None)
            event_id = "resolve:" + operation_id
            # (1) attempt_registry ownership (or a direct 'fenced' insert)
            registry_row = q.q("SELECT * FROM attempt_registry WHERE"
                               " operation_id=%s", (operation_id,), "one")
            ledger_rows = 1  # the resolved_accounting row itself
            if registry_row is not None and registry_row["state"] == \
                    "active":
                q.q("UPDATE attempt_registry SET state='fenced',"
                    " fenced_by=%s, fenced_at=%s, resolution_event_id=%s"
                    " WHERE operation_id=%s",
                    (resolver.value, now, event_id, operation_id), "exec")
            elif registry_row is None:
                q.q("INSERT INTO attempt_registry(operation_id, uid,"
                    " lifetime_id, attempt_id, session_id, commitment,"
                    " state, fenced_by, fenced_at, resolution_event_id,"
                    " minted_at) VALUES(%s,%s,%s,NULL,NULL,%s,'fenced',%s,"
                    "%s,%s,%s)",
                    (operation_id, owner_uid, lid, binding_hash,
                     resolver.value, now, event_id, now), "exec")
                q.q("INSERT INTO resolution_event_ledger("
                    " resolution_event_id, class_name, entry_id,"
                    " inserted_at) VALUES(%s,'attempt_registry',%s,%s)",
                    (event_id, operation_id, now), "exec")
                ledger_rows += 1
            # (2) attest EVERY surviving 'armed' witness
            attested = 0
            for w in armed:
                q.q("UPDATE abort_witnesses SET state='attested',"
                    " terminal_state='attested', attested_at=%s,"
                    " resolution_event_id=%s WHERE operation_id=%s AND"
                    " attempt_id=%s",
                    (now, event_id, operation_id, w["attempt_id"]), "exec")
                attested += 1
            # (3) resolution event + resolved_accounting + blocked
            # transition, SAME transaction
            self._write_resolution(owner_uid, lid, operation_id,
                                   recovery_id, binding_hash, "rolled_back",
                                   event_id, now, attested=attested,
                                   ledger_rows=ledger_rows)
            blocked = q.q("SELECT * FROM blocked_records WHERE uid=%s AND"
                          " lifetime_id=%s AND operation_id=%s",
                          (owner_uid, lid, operation_id), "one")
            if blocked is not None and blocked["state"] != "resolved":
                q.q("UPDATE blocked_records SET state='resolved' WHERE"
                    " recovery_id=%s", (blocked["recovery_id"],), "exec")
            self._uncertain.pop((owner_uid, lid, operation_id), None)
            scope = env.ModelOnlyOperationScope(uid=owner_uid, lifetime=lid,
                                                operation_id=operation_id)
            receipt = RolledBackReceipt(scope=scope,
                                        binding_hash=binding_hash,
                                        operation_id=operation_id,
                                        owner=Lifetime(uid=owner_uid,
                                                       lifetime_id=lid),
                                        fence=1, store_revision=None,
                                        outcome="rolled_back")
            self._read_commit()
            return ResolutionResult(
                kind="rolled_back", receipt=receipt,
                resolution_event_id=event_id, resolved_count=ledger_rows,
                attested_witness_count=attested,
                new_retained=1 + ledger_rows)

        except Exception:
            self._read_abort()
            raise

    def _write_resolution(self, uid, lid, operation_id, recovery_id,
                          binding_hash, resolution, event_id, now,
                          attested, ledger_rows: int = 1) -> None:
        source_json = _canon({
            "anchor": "resolved_accounting",
            "commitment": binding_hash,
            "operation_id": operation_id,
            "resolver": "manual"}).decode("utf-8")
        new_retained = 1 + ledger_rows
        self._q.q("INSERT INTO resolved_accounting(recovery_id, uid,"
                  " lifetime_id, operation_id, commitment, resolution,"
                  " source_json, resolution_event_id, resolved_count,"
                  " attested_witness_count, new_retained, created_at)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (recovery_id, uid, lid, operation_id, binding_hash,
                   resolution, source_json, event_id, ledger_rows,
                   attested, new_retained, now), "exec")
        self._q.q("INSERT INTO resolution_event_ledger(resolution_event_id,"
                  " class_name, entry_id, inserted_at)"
                  " VALUES(%s,'resolved_accounting',%s,%s)",
                  (event_id, recovery_id, now), "exec")
        # closing stamp: blocked-arrival evidence rows for this recovery
        # carry the EXACT associated resolved_accounting closing event
        # (sec 5; insertion_event_id stays NULL permanently)
        self._q.q("UPDATE evidence_entries SET resolution_event_id=%s"
                  " WHERE recovery_id=%s AND origin_kind='blocked_arrival'"
                  " AND resolution_event_id IS NULL",
                  (event_id, recovery_id), "exec")

    def _resolution_from_accounting(self, row, uid, lid, operation_id,
                                    record) -> ResolutionResult:
        if row["resolution"] == "committed":
            outcome = _decode_stored_outcome(record) if record is not None \
                and record["outcome_json"] is not None else None
            post = None
            if outcome is not None:
                post = PostReadResult(
                    original_outcome=outcome,
                    applied_at_revision=record["applied_at_revision"],
                    current_revision=max(self._store_revision(uid), 1),
                    current=self._historical_current_view(uid, lid, outcome))
            return ResolutionResult(
                kind="committed", post=post,
                resolution_event_id=row["resolution_event_id"],
                resolved_count=row["resolved_count"],
                attested_witness_count=row["attested_witness_count"],
                new_retained=row["new_retained"])
        scope = env.ModelOnlyOperationScope(uid=uid, lifetime=lid,
                                            operation_id=operation_id)
        return ResolutionResult(
            kind="rolled_back",
            receipt=RolledBackReceipt(scope=scope,
                                      binding_hash=row["commitment"],
                                      operation_id=operation_id,
                                      owner=Lifetime(uid=uid,
                                                     lifetime_id=lid),
                                      fence=1, store_revision=None,
                                      outcome="rolled_back"),
            resolution_event_id=row["resolution_event_id"],
            resolved_count=row["resolved_count"],
            attested_witness_count=row["attested_witness_count"],
            new_retained=row["new_retained"])

    # -- evidence insertion (the receiver side; sec 5 LOOKUP-BEFORE-MINT) ---

    def _insert_evidence_row(self, evidence_id, projection,
                             delivery_token, now, origin, owner_uid,
                             lifetime_id, read_operation_id, event_id):
        """The ONE evidence-row insert (both origins); the
        (recovery_id, delivery_token) UNIQUE fence backs the race."""
        self._q.q("INSERT INTO evidence_entries(evidence_id, recovery_id,"
                  " kind, delivery_token, received_at, ref,"
                  " projection_json, insertion_event_id,"
                  " resolution_event_id, origin_kind, inserting_uid,"
                  " inserting_lifetime_id, inserting_operation_id)"
                  " VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                  (evidence_id, projection.recovery_id,
                   projection.kind.value, delivery_token, now,
                   "evidence:" + evidence_id,
                   _canon(projection.to_dict()).decode("utf-8"),
                   event_id, None, origin, owner_uid, lifetime_id,
                   read_operation_id), "exec")

    def _evidence_unique_loser(self, projection, delivery_token):
        """UNIQUE-delivery conflict loser (v80 3931/3944): the contender
        WON the race - re-read the winner row and compare the
        projection: EQUAL = the loser's REPLAY (NO writes); CHANGED =
        typed drift REFUSAL. Runs AFTER the conflict's ROLLBACK."""
        winner = self._q.q("SELECT * FROM evidence_entries WHERE"
                           " recovery_id=%s AND delivery_token=%s",
                           (projection.recovery_id, delivery_token),
                           "one")
        if winner is None:
            raise cap.ConflictError(cap.ModelCode.REQUEST_DRIFT)
        stored = SafeEvidenceProjection.from_dict(
            json.loads(winner["projection_json"]))
        if stored.to_dict() != projection.to_dict():
            raise DeliveryDriftError()
        if winner["origin_kind"] == "blocked_arrival":
            return InsertionEventResult(
                kind="blocked_replay_no_event", contribution=0,
                evidence_id=winner["evidence_id"], stored_new_retained=1,
                resolution_event_id=winner["resolution_event_id"])
        event = self._q.q("SELECT * FROM insertion_events WHERE"
                          " event_id=%s",
                          (winner["insertion_event_id"],), "one")
        if event is None:
            raise EvidenceIntegrityError()
        # v80 1254-1308: the loser re-verifies the FULL coupling too
        _verify_insertion_event_coupling(self._q, event)
        return InsertionEventResult(
            kind="replayed", contribution=0,
            event_id=winner["insertion_event_id"],
            stored_new_retained=event["new_retained"])

    def insert_evidence(self, owner_uid: int, lifetime_id: int,
                        read_operation_id: str,
                        projection: SafeEvidenceProjection,
                        delivery_token: str,
                        capability: SessionBoundCapability,
                        context: "env.ModelOnlyCallerContext"
                        ) -> InsertionEventResult:
        """The receiving path: authenticate -> identity lookup (by token)
        -> projection compare -> EXISTING returns the stored variant with
        NO writes -> ONLY a NEW delivery classifies by CURRENT op state."""
        _req_int(owner_uid, 0)
        _req_int(lifetime_id, 1)
        _req_hex(read_operation_id, OP_ID_LEN)
        _req_hex(delivery_token, 32)
        if type(projection) is not SafeEvidenceProjection:
            raise cap.ModelInputError()
        if type(context) is not env.ModelOnlyCallerContext:
            raise cap.ModelInputError()
        inner = self._validate_handle(capability, SessionBoundCapability,
                                      "capability")
        if identity_mismatch(inner, owner_uid, lifetime_id):
            raise cap.AuthError()
        # the FULL receiver chain (sec 5): the capability MUST be a FRESH
        # REGISTERED READ setup BOUND TO THIS ACTUAL INPUT (minted via
        # the store's producer by the injected provider) - nothing cached
        registered = self._read_contexts.get(capability.handle)
        if registered is None:
            raise cap.AuthError()  # not a registered read handle
        read_targets = {"projection": projection.to_dict(),
                        "token": delivery_token}
        if context.original_binding.issuer != context.current_issuer:
            raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
        supplied = json.loads(_canon(_context_canon(context)))
        expected = _read_projection(self, ReadMethod.READBACK, owner_uid,
                                    lifetime_id, read_targets,
                                    registered["clock_reading"],
                                    supplied, capability.handle)
        if expected != capability.binding_hash:
            raise cap.AuthError()  # the setup is NOT bound to this input
        # SUBJECT hash-owner-accounting scope: the projection binds to
        # THIS owner accounting (a forged uid/lifetime NEVER lands)
        if projection.uid != owner_uid \
                or projection.lifetime_id != lifetime_id \
                or projection.accounting_scope != Lifetime(
                    uid=owner_uid, lifetime_id=lifetime_id):
            raise EvidenceScopeError()
        now = _clock_now(self._clock)
        conn = self._q.raw
        conn.execute("BEGIN IMMEDIATE")
        try:
            q = self._q
            # PhaseA (the REAL recheck) + the CURRENT issuer freshness,
            # INSIDE the insertion transaction (revoked grants refuse)
            issuer_row = q.q("SELECT issuer_json, valid FROM issuer_state"
                             " WHERE uid=%s", (owner_uid,), "one")
            if issuer_row is None or not issuer_row["valid"] \
                    or not context.current_issuer_valid:
                raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
            stored_issuer = _typed_issuer_from_json(
                issuer_row["issuer_json"])
            if stored_issuer is not None \
                    and stored_issuer != context.current_issuer:
                raise cap.ConflictError(cap.ModelCode.STALE_POLICY)
            cap_request = cap.Request(
                operation_id=inner.request.operation_id,
                operation_class=cap.OperationClass.READ,
                required_scopes=frozenset({cap.Scope.MEMORY_READ}),
                captured_epoch=None, requested_audience_id=None,
                permitted_information_ref=(
                    "read:" + ReadMethod.READBACK.value),
                extraction_intent=cap.ExtractionIntent.NOT_APPLICABLE,
                group_action=None, sensitive=False)
            decision = self._evaluator.recheck_model(inner, cap_request)
            if not decision.allowed:
                raise _phase_a_error(decision.code)
            existing = q.q("SELECT * FROM evidence_entries WHERE"
                           " recovery_id=%s AND delivery_token=%s",
                           (projection.recovery_id, delivery_token), "one")
            if existing is not None:
                # REPLAY: exact projection compare; DRIFT = typed refusal
                stored = SafeEvidenceProjection.from_dict(
                    json.loads(existing["projection_json"]))
                if stored.to_dict() != projection.to_dict():
                    conn.execute("ROLLBACK")
                    raise DeliveryDriftError()
                if existing["origin_kind"] == "blocked_arrival":
                    conn.execute("COMMIT")
                    return InsertionEventResult(
                        kind="blocked_replay_no_event", contribution=0,
                        evidence_id=existing["evidence_id"],
                        stored_new_retained=1,
                        resolution_event_id=
                        existing["resolution_event_id"])
                event = q.q("SELECT * FROM insertion_events WHERE"
                            " event_id=%s",
                            (existing["insertion_event_id"],), "one")
                if event is None:
                    conn.execute("ROLLBACK")
                    raise EvidenceIntegrityError()
                try:
                    # v80 1254-1308: the FULL one-to-one per-class
                    # coupling RE-VERIFIED at replay - never a default
                    _verify_insertion_event_coupling(q, event)
                except EvidenceIntegrityError:
                    conn.execute("ROLLBACK")
                    raise
                conn.execute("COMMIT")
                return InsertionEventResult(
                    kind="replayed", contribution=0,
                    event_id=existing["insertion_event_id"],
                    stored_new_retained=event["new_retained"])
            evidence_id = secrets.token_hex(16)
            # classify by the CURRENT op state: unresolved = blocked
            # branch (row ONLY, NO event); resolved = late branch (one
            # insertion event + one ledger row)
            resolved = q.q("SELECT * FROM resolved_accounting"
                           " WHERE recovery_id=%s",
                           (projection.recovery_id,), "one")
            origin = "late_insert" if resolved is not None \
                else "blocked_arrival"
            event_id = None
            if origin == "late_insert":
                # the TARGET JOIN: the resolved anchor's owner/operation/
                # commitment MUST join to the owner accounting
                if resolved["uid"] != owner_uid \
                        or resolved["lifetime_id"] != lifetime_id \
                        or resolved["operation_id"] != projection.operation_id \
                        or resolved["commitment"] != projection.commitment:
                    conn.execute("ROLLBACK")
                    raise EvidenceScopeError()
                # STORE-BUILT target/source binding (v80 795-812): an
                # UNRELATED same-owner inventory target REFUSES
                if not _evidence_target_source_ok(
                        q, projection, resolved["uid"],
                        resolved["lifetime_id"],
                        resolved["operation_id"]):
                    conn.execute("ROLLBACK")
                    raise EvidenceScopeError()
            if origin == "blocked_arrival":
                # EVIDENCE OVERFLOW (sec 5): count freezes at 64; the
                # overflow marker flips to 1 and STAYS; a delivery past
                # the cap is RECEIVED-REFUSED (the marker row is proof)
                blocked_row = q.q(
                    "SELECT uid, lifetime_id, operation_id, binding_hash,"
                    " state, evidence_count, overflow_marker FROM"
                    " blocked_records WHERE recovery_id=%s",
                    (projection.recovery_id,), "one")
                # CURRENT-RECOVERY EXISTENCE: the recovery_id MUST name a
                # CURRENT blocked record (a nonexistent recovery NEVER
                # lands - the typed unknown-recovery refusal)
                if blocked_row is None or blocked_row["state"] == "resolved":
                    conn.execute("ROLLBACK")
                    raise UnknownRecoveryError()
                # the TARGET JOIN: the blocked record's owner/operation/
                # commitment MUST join to the owner accounting
                if blocked_row["uid"] != owner_uid \
                        or blocked_row["lifetime_id"] != lifetime_id \
                        or blocked_row["operation_id"] != projection.operation_id \
                        or blocked_row["binding_hash"] != projection.commitment:
                    conn.execute("ROLLBACK")
                    raise EvidenceScopeError()
                # STORE-BUILT target/source binding (v80 795-812): an
                # UNRELATED same-owner inventory target REFUSES
                if not _evidence_target_source_ok(
                        q, projection, blocked_row["uid"],
                        blocked_row["lifetime_id"],
                        blocked_row["operation_id"]):
                    conn.execute("ROLLBACK")
                    raise EvidenceScopeError()
                if blocked_row["evidence_count"] >= 64:
                    if blocked_row["overflow_marker"] != 1:
                        q.q("UPDATE blocked_records SET"
                            " overflow_marker=1 WHERE recovery_id=%s",
                            (projection.recovery_id,), "exec")
                    conn.execute("COMMIT")
                    raise cap.CapRefused(65)
                q.q("UPDATE blocked_records SET"
                    " evidence_count=evidence_count+1 WHERE"
                    " recovery_id=%s", (projection.recovery_id,),
                    "exec")
                try:
                    self._insert_evidence_row(
                        evidence_id, projection, delivery_token, now,
                        origin, owner_uid, lifetime_id,
                        read_operation_id, None)
                except sqlite3.IntegrityError:
                    # UNIQUE delivery conflict: a concurrent contender
                    # WON - rollback, re-read the winner, compare:
                    # EQUAL = the loser's replay; CHANGED = typed drift
                    # refusal (v80 3944)
                    conn.execute("ROLLBACK")
                    return self._evidence_unique_loser(projection,
                                                       delivery_token)
                conn.execute("COMMIT")
                return InsertionEventResult(kind="blocked_inserted",
                                            contribution=1,
                                            evidence_id=evidence_id)
            event_id = "insert:" + evidence_id
            try:
                self._insert_evidence_row(
                    evidence_id, projection, delivery_token, now,
                    origin, owner_uid, lifetime_id, read_operation_id,
                    event_id)
            except sqlite3.IntegrityError:
                conn.execute("ROLLBACK")
                return self._evidence_unique_loser(projection,
                                                   delivery_token)
            q.q("INSERT INTO insertion_events(event_id,"
                " subject_evidence_ref, recovery_id,"
                " inserting_operation_id, inserting_uid,"
                " inserting_lifetime_id, uid, lifetime_id, operation_id,"
                " commitment, source, inserted_count, new_retained,"
                " created_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,3,"
                "%s)",
                (event_id, "evidence:" + evidence_id,
                 projection.recovery_id, read_operation_id, owner_uid,
                 lifetime_id, projection.uid, projection.lifetime_id,
                 projection.operation_id, projection.commitment,
                 projection.source.value, now), "exec")
            q.q("INSERT INTO resolution_event_ledger(resolution_event_id,"
                " class_name, entry_id, inserted_at)"
                " VALUES(%s,'evidence_entries',%s,%s)",
                (event_id, evidence_id, now), "exec")
            conn.execute("COMMIT")
            return InsertionEventResult(kind="inserted", contribution=3,
                                        event_id=event_id)
        except (DeliveryDriftError, cap.CapRefused,
                UnknownRecoveryError, EvidenceScopeError,
                EvidenceIntegrityError):
            raise  # typed refusals that ALREADY rolled back at their
            # raise site - NEVER a second ROLLBACK (OperationalError)
        except Exception:
            conn.execute("ROLLBACK")
            raise


FactStore._historical_targets = _Historical._historical_targets
FactStore._historical_load = _Historical._historical_load
FactStore.readback = _Historical.readback
FactStore._historical_current_view = _Historical._historical_current_view
FactStore.resolve = _Historical.resolve
FactStore._write_resolution = _Historical._write_resolution
FactStore._resolution_from_accounting = (
    _Historical._resolution_from_accounting)
FactStore.insert_evidence = _Historical.insert_evidence
FactStore._insert_evidence_row = _Historical._insert_evidence_row
FactStore._evidence_unique_loser = _Historical._evidence_unique_loser


# ---------------------------------------------------------------------------
# PRODUCTION STUBS (sec 1) - REFUSE UNCONDITIONALLY, before DI
# ---------------------------------------------------------------------------

class ProductionFactStore:
    """The PRODUCTION path is a STUB: every surface refuses
    ProductionStubRefused / MODEL_ONLY unconditionally."""

    def __init__(self, *args, **kwargs) -> None:
        raise cap.ProductionStubRefused()

    def __getattr__(self, _name):
        raise cap.ProductionStubRefused()


class SensitivePurgeStub:
    """The sensitive-purge feature stub: BLOCKED unconditionally."""

    def __init__(self, *args, **kwargs) -> None:
        raise cap.ProductionStubRefused()

    def __getattr__(self, _name):
        raise cap.ProductionStubRefused()
