"""Tests for PR-2 schema additions - DESIGN-MODEL ONLY, local disposable.

Scope (peer-accepted v4.2): docs + synthetic/local disposable tests.
This file proves NOTHING about Postgres ALTER/locking/encryption/auth/
erasure. There are NO shipped module SQL strings in PR-2 (no source
helper exists); the sqlite mirror below HAND-TRANSLATES the proposed
DDL from docs/memory/pr2-schema.md with these dialect adaptations:
- btrim()          -> trim()
- BYTEA            -> BLOB
- digest regex CHECK '~' (Postgres) -> length=64 CHECK + a python-layer
  hex validator WIRED INTO the test-local write boundary (direct raw
  SQL bypasses it - dialect gap stated honestly and exercised by one
  explicit test; the validator-protected model path is the contract)
- TIMESTAMPTZ DEFAULT now() -> TEXT DEFAULT CURRENT_TIMESTAMP
Unicode/runtime assumptions: NFC results are pinned to THIS runtime's
unicodedata (Python 3, Unicode version reported by the test run);
cross-runtime NFC equivalence (other ICU/Unicode versions) is an OPEN
gate, NOT claimed here.
content_payload is SYNTHETIC OPAQUE MOCK BYTES - never parsed, never
encryption proof. All keys/inputs are synthetic public test values.
"""

import hashlib
import hmac
import os
import sqlite3
import struct
import sys
import unittest
import unicodedata

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

MARKER_INPUT_MAX_BYTES = 4096  # frozen marker-input limit (sec 5);
                               # NOT the 500-codepoint provenance budget
NORM_VERSION = "nfc-lf-v1"
FRAME_DOMAIN = b"crayon.memory-marker.v1\x00"
BIGINT_MAX = 9223372036854775807

# synthetic public vector constants (peer-supplied, compute-verified)
VKEY = bytes(range(32))          # K = bytes 00..1f
VUID = 76
EXPECTED_1 = ("7a82df294b89d711e393293622f2878d3241a21e32599222c69d4cc3"
              "683f6d57")
EXPECTED_2 = ("bb25bf1063b468cfa81d4aa20804e42b7460bbc02e66b1357ec9f10"
              "118b40d9a")


class DesignError(Exception):
    """Design-model rejection (fail closed)."""


class CASMismatch(DesignError):
    """Explicit CAS conflict - never silent."""


class VersionOverflow(DesignError):
    """Signed BIGINT row_version overflow - fail closed, row unchanged."""


# ----- design-model helpers (TEST-local, NOT shipped source) -----

def require_version(value, what):
    """Shared exact-version validator: BOTH expected and stored
    versions must be exact non-bool ints >= 0. bool True/False and
    float 1.0 are REJECTED (True == 1 and 1.0 == 1 would otherwise
    silently pass a CAS compare)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise DesignError("%s must be an exact non-bool int >= 0"
                          % (what,))
    return value


def canon_uid(uid):
    """uid -> canonical ASCII decimal bytes. Validated EXACT int only:
    no bool, no float, no str, no leading zeros, no coercion."""
    if isinstance(uid, bool) or not isinstance(uid, int):
        raise DesignError("uid must be an exact int")
    return str(uid).encode("ascii")


def normalize_nfc_lf_v1(text):
    """nfc-lf-v1: str required; strict UTF-8 (python str enforces);
    CRLF and lone CR -> LF; NFC over the whole text. PRESERVES case,
    accents, punctuation, tabs, spaces, zero-width chars. Rejects
    empty / non-string / lone surrogates / oversize WITHOUT echoing
    content. NEVER truncates."""
    if not isinstance(text, str):
        raise DesignError("marker input must be str")
    try:
        text.encode("utf-8")  # strict: lone surrogates raise
    except UnicodeEncodeError:
        raise DesignError("marker input not strict UTF-8")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = unicodedata.normalize("NFC", text)
    out = text.encode("utf-8")
    if len(out) == 0:
        raise DesignError("marker input empty")
    if len(out) > MARKER_INPUT_MAX_BYTES:
        raise DesignError("marker input oversize")
    return out


def marker_frame(uid, profile, normalized_utf8):
    """frame = domain + u32be(len) + uid + u32be(len) + profile
    + u64be(len) + normalized. ALL lengths are BYTE lengths."""
    u = canon_uid(uid)
    p = profile.encode("utf-8") if isinstance(profile, str) else None
    if not p:
        raise DesignError("unknown normalization profile")
    return (FRAME_DOMAIN
            + struct.pack(">I", len(u)) + u
            + struct.pack(">I", len(p)) + p
            + struct.pack(">Q", len(normalized_utf8)) + normalized_utf8)


def marker_digest(key, uid, text, profile=NORM_VERSION):
    """HMAC-SHA256(key, frame) as 64 lowercase hex. Missing key or
    unknown profile FAILS CLOSED - there is NO unkeyed fallback."""
    if not isinstance(key, (bytes, bytearray)) or len(key) != 32:
        raise DesignError("marker key missing or malformed")
    if profile != NORM_VERSION:
        raise DesignError("unknown normalization profile")
    norm = normalize_nfc_lf_v1(text)
    return hmac.new(bytes(key), marker_frame(uid, profile, norm),
                    hashlib.sha256).hexdigest()


def validate_digest_hex(digest):
    """Python-layer ACTUAL hex validation (mirrors the proposed
    Postgres regex CHECK '^[0-9a-f]{64}$'). WIRED INTO the test-local
    marker write boundary (insert_marker) - the model path never
    persists an unvalidated digest."""
    if not isinstance(digest, str) or len(digest) != 64:
        raise DesignError("digest must be 64 hex chars")
    if any(c not in "0123456789abcdef" for c in digest):
        raise DesignError("digest must be lowercase hex")
    return digest


# ----- sqlite design-model mirror -----

SCHEMA = (
    # legacy facts shape (packet cr_db.py:22) + the TWO additive
    # column proposals (row_version, provenance_payload)
    "CREATE TABLE facts(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL,"
    " key TEXT NOT NULL, value TEXT NOT NULL,"
    " category TEXT DEFAULT 'general', source TEXT DEFAULT 'chat',"
    " updated_at TEXT DEFAULT CURRENT_TIMESTAMP,"
    " row_version INTEGER NOT NULL DEFAULT 0,"
    " provenance_payload BLOB,"
    " UNIQUE(user_id, key), CHECK (row_version >= 0))",
    "CREATE TABLE fact_history(id INTEGER PRIMARY KEY,"
    " user_id INTEGER NOT NULL,"
    " owner_lifetime INTEGER NOT NULL CHECK (owner_lifetime >= 1),"
    " fact_id INTEGER NOT NULL,"
    " source_row_version INTEGER NOT NULL CHECK (source_row_version >= 0),"
    " closed_reason TEXT NOT NULL"
    " CHECK (closed_reason IN ('supersede','consolidate')),"
    " superseded_by_version INTEGER NOT NULL"
    " CHECK (superseded_by_version >= 1),"
    " operation_id TEXT NOT NULL CHECK (length(trim(operation_id)) > 0),"
    " replacement_fact_id INTEGER NOT NULL,"
    " archived_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
    " content_payload BLOB NOT NULL,"
    " UNIQUE(user_id, owner_lifetime, fact_id, source_row_version))",
    "CREATE TABLE memory_markers(id INTEGER PRIMARY KEY,"
    " user_id INTEGER NOT NULL,"
    " marker_type TEXT NOT NULL"
    " CHECK (marker_type IN ('erased_content','excluded_content')),"
    " key_id TEXT NOT NULL CHECK (length(trim(key_id)) > 0),"
    " normalization_version TEXT NOT NULL"
    " CHECK (length(trim(normalization_version)) > 0),"
    " content_key_digest TEXT NOT NULL"
    " CHECK (length(content_key_digest) = 64),"
    " created_epoch INTEGER NOT NULL CHECK (created_epoch >= 1),"
    " created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,"
    " row_version INTEGER NOT NULL DEFAULT 0 CHECK (row_version >= 0),"
    " UNIQUE(user_id, marker_type, key_id, normalization_version,"
    " content_key_digest))",
)


def fresh_db(path):
    if os.path.exists(path):
        os.remove(path)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    for stmt in SCHEMA:
        conn.execute(stmt)
    return conn


def insert_marker(db, uid, mtype, key_id, nver, digest, epoch):
    """TEST-LOCAL WRITE BOUNDARY for markers: the digest is validated
    (validate_digest_hex) BEFORE persisting - the model path never
    stores an unvalidated digest. (Direct raw SQL bypasses this; that
    dialect gap is exercised explicitly in its own test.)"""
    validate_digest_hex(digest)
    db.execute(
        "INSERT INTO memory_markers(user_id,marker_type,key_id,"
        "normalization_version,content_key_digest,created_epoch)"
        " VALUES (?,?,?,?,?,?)",
        (uid, mtype, key_id, nver, digest, epoch))


class DBCase(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__),
                                 ".pr2_test.sqlite")
        self.db = fresh_db(self.path)

    def tearDown(self):
        self.db.close()
        if os.path.exists(self.path):
            os.remove(self.path)


class TestSchemaLifecycle(DBCase):
    def test_fresh_apply_and_drop(self):
        for t in ("facts", "fact_history", "memory_markers"):
            self.assertIsNotNone(self.db.execute(
                "SELECT name FROM sqlite_master WHERE name=?", (t,)
            ).fetchone())
        for t in ("fact_history", "memory_markers"):
            self.db.execute("DROP TABLE %s" % t)
        for t in ("fact_history", "memory_markers"):
            self.assertIsNone(self.db.execute(
                "SELECT name FROM sqlite_master WHERE name=?", (t,)
            ).fetchone())

    def test_legacy_row_reads_defaults(self):
        """A row inserted without the new columns reads row_version 0
        and provenance_payload NULL (NULL = UNKNOWN, never backfill)."""
        self.db.execute(
            "INSERT INTO facts(user_id,key,value) VALUES (?,?,?)",
            (1, "color", "blue"))
        row = self.db.execute(
            "SELECT row_version, provenance_payload FROM facts"
            " WHERE user_id=1").fetchone()
        self.assertEqual(row["row_version"], 0)
        self.assertIsNone(row["provenance_payload"])

    def test_row_version_check_rejects_negative(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.execute(
                "INSERT INTO facts(user_id,key,value,row_version)"
                " VALUES (?,?,?,?)", (1, "k", "v", -1))


class TestRowVersionModel(DBCase):
    """Design-model writer probes: exact-version validation on BOTH
    expected and stored versions, strict increase, identical replay
    unchanged, explicit CAS mismatch, typed fail-closed overflow.
    The writer itself is a LATER PR; this models its contract."""

    def _write(self, uid, key, value, expected_version):
        require_version(expected_version, "expected_version")
        row = self.db.execute(
            "SELECT id, value, row_version FROM facts"
            " WHERE user_id=? AND key=?", (uid, key)).fetchone()
        if row is None:
            if expected_version != 0:
                # no silent create under a nonzero expectation
                raise CASMismatch("no row exists; expected_version"
                                  " must be 0")
            # seam-created rows start at version 1; 0 = legacy only
            self.db.execute(
                "INSERT INTO facts(user_id,key,value,row_version)"
                " VALUES (?,?,?,1)", (uid, key, value))
            return 1
        stored = require_version(row["row_version"], "stored row_version")
        if stored != expected_version:
            raise CASMismatch("expected %r, found %r"
                              % (expected_version, stored))
        if row["value"] == value:
            return stored  # identical replay: UNCHANGED
        if stored >= BIGINT_MAX:
            raise VersionOverflow("row_version at signed BIGINT max")
        self.db.execute("UPDATE facts SET value=?, row_version=?"
                        " WHERE id=?", (value, stored + 1, row["id"]))
        return stored + 1

    def test_strict_increase_and_identical_replay(self):
        self.assertEqual(self._write(1, "k", "v1", 0), 1)
        self.assertEqual(self._write(1, "k", "v2", 1), 2)
        self.assertEqual(self._write(1, "k", "v2", 2), 2)  # replay

    def test_cas_mismatch_is_explicit(self):
        self._write(1, "k", "v1", 0)
        with self.assertRaises(CASMismatch):
            self._write(1, "k", "v2", 7)

    def test_writer_rejects_inexact_expected_versions(self):
        """Writer-level rejection (NOT self-raised): True, 1.0, -1,
        'x', None never reach the CAS compare."""
        self._write(1, "k", "v1", 0)  # row at version 1
        for bad in (True, False, 1.0, 0.0, -1, "1", None):
            with self.assertRaises(DesignError, msg=repr(bad)):
                self._write(1, "k", "v2", bad)
        row = self.db.execute("SELECT value, row_version FROM facts"
                              " WHERE user_id=1 AND key='k'").fetchone()
        self.assertEqual((row["value"], row["row_version"]), ("v1", 1))

    def test_writer_rejects_corrupt_stored_version(self):
        """A corrupted stored version (TEXT in the INTEGER column)
        fails closed at the writer, never coerced."""
        self._write(1, "k", "v1", 0)
        self.db.execute("UPDATE facts SET row_version='abc'"
                        " WHERE user_id=1 AND key='k'")
        with self.assertRaises(DesignError):
            self._write(1, "k", "v2", 1)

    def test_no_silent_create_without_row(self):
        with self.assertRaises(CASMismatch):
            self._write(1, "ghost", "v", 5)
        self.assertIsNone(self.db.execute(
            "SELECT 1 FROM facts WHERE user_id=1 AND key='ghost'"
        ).fetchone())

    def test_overflow_fails_closed_row_unchanged(self):
        self._write(1, "k", "v1", 0)
        self.db.execute("UPDATE facts SET row_version=?"
                        " WHERE user_id=1 AND key='k'", (BIGINT_MAX,))
        with self.assertRaises(VersionOverflow):
            self._write(1, "k", "v2", BIGINT_MAX)
        row = self.db.execute("SELECT value, row_version FROM facts"
                              " WHERE user_id=1 AND key='k'").fetchone()
        self.assertEqual((row["value"], row["row_version"]),
                         ("v1", BIGINT_MAX))


class TestFactHistory(DBCase):
    def _insert(self, uid=1, lifetime=100, fact_id=10, srv=0,
                reason="supersede", sbv=1, op="op-1", repl=99,
                payload=b"\x00mock-envelope-not-encryption\x01"):
        self.db.execute(
            "INSERT INTO fact_history(user_id,owner_lifetime,fact_id,"
            "source_row_version,closed_reason,superseded_by_version,"
            "operation_id,replacement_fact_id,content_payload)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (uid, lifetime, fact_id, srv, reason, sbv, op, repl,
             payload))

    def test_round_trip_and_opaque_payload(self):
        blob = b"\x00mock-envelope-not-encryption\x01"
        self._insert(payload=blob)
        row = self.db.execute("SELECT * FROM fact_history").fetchone()
        self.assertEqual(bytes(row["content_payload"]), blob)  # opaque,
        # byte-identical, NEVER parsed

    def test_closed_reason_check(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(reason="erased")  # deletion-content history
        # is NEVER inserted; the enum carries supersede/consolidate only

    def test_superseded_by_version_floor(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(sbv=0)  # fresh replacements start at 1

    def test_replacement_referent_required(self):
        """Both supported closing reasons close INTO a replacement:
        NULL replacement, lifetime < 1, and blank operation_id are
        refused by the schema itself."""
        for reason in ("supersede", "consolidate"):
            with self.assertRaises((sqlite3.IntegrityError,
                                    sqlite3.OperationalError),
                                   msg=reason):
                self.db.execute(
                    "INSERT INTO fact_history(user_id,owner_lifetime,"
                    "fact_id,source_row_version,closed_reason,"
                    "superseded_by_version,operation_id,"
                    "replacement_fact_id,content_payload)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (1, 100, 10, 0, reason, 1, "op-x", None, b"\x00"))
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(lifetime=0)
        for bad_op in ("", "   "):
            with self.assertRaises(sqlite3.IntegrityError, msg=repr(bad_op)):
                self._insert(op=bad_op)

    def test_snapshot_uniqueness(self):
        self._insert(srv=0)
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert(srv=0)  # (owner,lifetime,fact,src_version) dup
        self._insert(srv=1)  # next version is a new snapshot

    def test_n_to_1_junction_with_real_replacement(self):
        """Meaningful N-to-1: the replacement fact row EXISTS at
        version 1 (seam-created) with the SAME owner + lifetime the
        history rows reference. No global ordering is asserted across
        different facts' versions."""
        self.db.execute(
            "INSERT INTO facts(id,user_id,key,value,row_version)"
            " VALUES (99,1,'merged','v',1)")
        self._insert(fact_id=10, srv=0, reason="consolidate", sbv=1,
                     op="op-merge-1", repl=99)
        self._insert(fact_id=11, srv=0, reason="consolidate", sbv=1,
                     op="op-merge-1", repl=99)
        rows = self.db.execute(
            "SELECT h.fact_id, h.owner_lifetime, f.row_version"
            " FROM fact_history h JOIN facts f"
            " ON f.id = h.replacement_fact_id"
            " AND f.user_id = h.user_id"
            " WHERE h.operation_id='op-merge-1'").fetchall()
        self.assertEqual({r["fact_id"] for r in rows}, {10, 11})
        for r in rows:
            self.assertEqual(r["owner_lifetime"], 100)
            self.assertEqual(r["row_version"], 1)  # the referenced
            # replacement's post-commit version


class TestMarkers(DBCase):
    DIG = EXPECTED_1

    def test_uniqueness_tuple(self):
        insert_marker(self.db, 1, "erased_content", "mk1",
                      NORM_VERSION, self.DIG, 1)
        with self.assertRaises(sqlite3.IntegrityError):
            insert_marker(self.db, 1, "erased_content", "mk1",
                          NORM_VERSION, self.DIG, 1)
        insert_marker(self.db, 2, "erased_content", "mk1",
                      NORM_VERSION, self.DIG, 1)  # different owner: fine

    def test_nonblank_key_and_version_checks(self):
        for bad in ("", "   "):
            with self.assertRaises(sqlite3.IntegrityError, msg=repr(bad)):
                insert_marker(self.db, 1, "erased_content", bad,
                              NORM_VERSION, self.DIG, 1)
            with self.assertRaises(sqlite3.IntegrityError, msg=repr(bad)):
                insert_marker(self.db, 1, "erased_content", "mk1",
                              bad, self.DIG, 1)

    def test_created_epoch_floor(self):
        with self.assertRaises(sqlite3.IntegrityError):
            insert_marker(self.db, 1, "erased_content", "mk1",
                          NORM_VERSION, self.DIG, 0)

    def test_malformed_digests_not_persisted(self):
        """The write boundary validates BEFORE persisting: malformed
        digests raise and the table stays empty."""
        for bad in ("g" * 64, "A" * 64, "a" * 63, "a" * 65, "", " " * 64):
            with self.assertRaises(DesignError, msg=repr(bad)):
                insert_marker(self.db, 1, "erased_content", "mk1",
                              NORM_VERSION, bad, 1)
        self.assertEqual(self.db.execute(
            "SELECT count(*) c FROM memory_markers").fetchone()["c"], 0)

    def test_direct_sql_dialect_gap_documented(self):
        """DIALECT CAVEAT, kept SEPARATE from the validator-protected
        model path: raw SQL bypasses validate_digest_hex, and the
        sqlite mirror CHECK is length-only (no regexp), so 'g'*64
        passes HERE. The proposed Postgres DDL carries the actual
        regex CHECK; this test pins the mirror gap so it is never
        mistaken for the contract."""
        self.db.execute(
            "INSERT INTO memory_markers(user_id,marker_type,key_id,"
            "normalization_version,content_key_digest,created_epoch)"
            " VALUES (?,?,?,?,?,?)",
            (1, "erased_content", "mk1", NORM_VERSION, "g" * 64, 1))
        self.assertEqual(self.db.execute(
            "SELECT count(*) c FROM memory_markers").fetchone()["c"], 1)


class TestNormalization(unittest.TestCase):
    def test_crlf_and_cr_become_lf(self):
        self.assertEqual(normalize_nfc_lf_v1("a\r\nb\rc"), b"a\nb\nc")

    def test_nfc_composes(self):
        self.assertEqual(normalize_nfc_lf_v1("Café\r\nTea"),
                         normalize_nfc_lf_v1("Café\nTea"))

    def test_preserves_case_whitespace_zero_width(self):
        raw = "A  b\tCé\u200bD"  # spaces, tab, zero-width space kept
        self.assertEqual(normalize_nfc_lf_v1(raw),
                         raw.encode("utf-8"))

    def test_rejects_without_echoing(self):
        secret = "dont-look-at-me"
        # every case MUST reject; the secret-bearing case proves the
        # rejection never echoes input content
        cases = ["", 123, None, b"bytes", "bad-surrogate-\ud800",
                 "x" * (MARKER_INPUT_MAX_BYTES + 1), secret + "\ud800"]
        for c in cases:
            with self.assertRaises(DesignError, msg=type(c).__name__) as cm:
                normalize_nfc_lf_v1(c)
            self.assertNotIn(secret, str(cm.exception))

    def test_oversize_boundary(self):
        self.assertEqual(len(normalize_nfc_lf_v1(
            "x" * MARKER_INPUT_MAX_BYTES)), MARKER_INPUT_MAX_BYTES)
        with self.assertRaises(DesignError):
            normalize_nfc_lf_v1("x" * (MARKER_INPUT_MAX_BYTES + 1))


class TestUidFraming(unittest.TestCase):
    def test_canonical_ascii_decimal(self):
        self.assertEqual(canon_uid(76), b"76")
        self.assertEqual(canon_uid(0), b"0")

    def test_rejects_non_exact(self):
        for bad in (True, False, 76.0, "76", "076", b"76", None):
            with self.assertRaises(DesignError, msg=repr(bad)):
                canon_uid(bad)


class TestHMACVectors(unittest.TestCase):
    def test_peer_vector_1_both_inputs(self):
        for raw in ("Café\r\nTea", "Café\nTea"):
            self.assertEqual(marker_digest(VKEY, VUID, raw), EXPECTED_1)

    def test_peer_vector_2_case_sensitive(self):
        self.assertEqual(marker_digest(VKEY, VUID, "café\nTea"),
                         EXPECTED_2)

    def test_rfc4231_case1(self):
        """RFC 4231 test case 1: key=0x0b*20, data='Hi There'."""
        self.assertEqual(
            hmac.new(b"\x0b" * 20, b"Hi There",
                     hashlib.sha256).hexdigest(),
            "b0344c61d8db38535ca8afceaf0bf12b881dc200c9833da726e9376c2"
            "e32cff7")

    def test_unicode_version_reported(self):
        """Pinned runtime assumption: report the unicodedata version
        these NFC results were computed under (cross-runtime NFC
        equivalence is an OPEN gate, not claimed)."""
        self.assertRegex(unicodedata.unidata_version, r"^\d+\.\d+\.\d+$")

    def test_constant_32_byte_output(self):
        for text in ("a", "Café\nTea", "x" * 1000):
            d = marker_digest(VKEY, VUID, text)
            self.assertEqual(len(d), 64)
            self.assertEqual(len(bytes.fromhex(d)), 32)

    def test_owner_key_profile_separation(self):
        base = marker_digest(VKEY, VUID, "Café\nTea")
        self.assertNotEqual(marker_digest(VKEY, 77, "Café\nTea"), base)
        self.assertNotEqual(marker_digest(bytes(range(1, 33)), VUID,
                                          "Café\nTea"), base)
        with self.assertRaises(DesignError):
            marker_digest(VKEY, VUID, "Café\nTea", profile="nfc-lf-v2")

    def test_missing_key_fails_closed_no_fallback(self):
        for bad in (None, b"", b"short", "00" * 32, 0):
            with self.assertRaises(DesignError, msg=repr(bad)):
                marker_digest(bad, VUID, "Café\nTea")

    def test_epoch_is_not_a_digest_input(self):
        """Changed epoch, same match: created_epoch is metadata only -
        nothing in the frame carries it, so older exclusions survive
        epoch movement by construction."""
        self.assertEqual(marker_digest(VKEY, VUID, "Café\nTea"),
                         EXPECTED_1)  # no epoch argument exists


class TestExclusionSurvivalModel(DBCase):
    def test_older_exclusions_match_across_epoch_movement(self):
        """Design model: a marker written at epoch 3 still matches at
        epoch 9 - created_epoch is never a matching filter."""
        dig = marker_digest(VKEY, VUID, "Café\nTea")
        insert_marker(self.db, VUID, "erased_content", "mk1",
                      NORM_VERSION, dig, 3)
        for observed_epoch in (3, 9):  # epoch "moves"; matching ignores it
            row = self.db.execute(
                "SELECT 1 FROM memory_markers WHERE user_id=? AND"
                " marker_type=? AND key_id=? AND normalization_version=?"
                " AND content_key_digest=?",
                (VUID, "erased_content", "mk1", NORM_VERSION,
                 marker_digest(VKEY, VUID, "Café\nTea"))).fetchone()
            self.assertIsNotNone(row,
                                 msg="epoch %d must not hide it"
                                 % observed_epoch)


if __name__ == "__main__":
    unittest.main(verbosity=2)
