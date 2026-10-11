"""Tests for PR-1 primitives: monotone epoch allocation + grants store.

LOCAL DISPOSABLE SQL ONLY (peer-agreed scope): these tests run against
an in-process sqlite shim mirroring the proposed Postgres shapes
(docs/memory/pr1-schema.md). No hosted or external database is touched.
The shim translates the modules' Postgres paramstyle (%s) to sqlite's
(?) so the SHIPPED SQL strings are what execute, not test-only copies.
The same suite is written to be re-pointed at the real SQL impl later
under a separate test-schema authority.
"""

import os
import sys
import sqlite3
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import owner_counters
import grants


class SqliteShimDB:
    """Minimal q(sql, params, mode) contract over sqlite (dict rows).

    mode: "one" -> dict|None, "all" -> list[dict], "none" -> None.
    Each q() call commits by itself; PR-1 callers own transactions, so
    the shim's autocommit only models single-statement atomicity, which
    is what these primitives rely on (allocation is ONE statement).
    """

    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, timeout=10,
                                    isolation_level=None)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=10000")
        self._in_tx = False

    def begin(self):
        """Explicit transaction for the atomicity tests. PR-1 callers
        own transactions in production; the shim models that."""
        self.conn.execute("BEGIN")
        self._in_tx = True

    def commit(self):
        self.conn.execute("COMMIT")
        self._in_tx = False

    def rollback(self):
        self.conn.execute("ROLLBACK")
        self._in_tx = False

    def q(self, sql, params=(), mode="all"):
        cur = self.conn.execute(sql.replace("%s", "?"), tuple(params))
        if mode == "none":
            return None
        rows = [dict(r) for r in cur.fetchall()]
        if mode == "one":
            return rows[0] if rows else None
        return rows


SCHEMA = (
    # mirrors docs/memory/pr1-schema.md incl. its CHECK constraints
    # (sqlite dialect: trim() for Postgres btrim())
    "CREATE TABLE owner_counters(user_id INTEGER PRIMARY KEY,"
    " next_epoch INTEGER NOT NULL CHECK (next_epoch >= 1))",
    "CREATE TABLE owner_grants(user_id INTEGER NOT NULL,"
    " scope TEXT NOT NULL, granted_at INTEGER NOT NULL,"
    " expires_at INTEGER, per_op_expiry_optin INTEGER NOT NULL DEFAULT 0,"
    " PRIMARY KEY (user_id, scope),"
    " CHECK (length(trim(scope)) > 0),"
    " CHECK (granted_at >= 0),"
    " CHECK (expires_at IS NULL OR expires_at >= 0))",
    "CREATE TABLE grant_generations(user_id INTEGER PRIMARY KEY,"
    " generation INTEGER NOT NULL CHECK (generation >= 0))",
    # stand-in for the app's content table, to prove counter survival
    "CREATE TABLE users(user_id INTEGER PRIMARY KEY,"
    " memory_epoch INTEGER NOT NULL DEFAULT 0)",
)


def fresh_db(path):
    if os.path.exists(path):
        os.remove(path)
    db = SqliteShimDB(path)
    for stmt in SCHEMA:
        db.q(stmt, (), "none")
    return db


class TestMonotoneEpoch(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__),
                                 ".pr1_test.sqlite")
        self.db = fresh_db(self.path)

    def tearDown(self):
        self.db.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_first_allocation_is_one_then_strictly_increases(self):
        a = owner_counters.allocate_next_epoch(self.db, 1001)
        b = owner_counters.allocate_next_epoch(self.db, 1001)
        c = owner_counters.allocate_next_epoch(self.db, 1001)
        self.assertEqual((a, b, c), (1, 2, 3))

    def test_per_owner_isolation(self):
        self.assertEqual(owner_counters.allocate_next_epoch(self.db, 1), 1)
        self.assertEqual(owner_counters.allocate_next_epoch(self.db, 2), 1)
        self.assertEqual(owner_counters.allocate_next_epoch(self.db, 1), 2)
        self.assertEqual(owner_counters.allocate_next_epoch(self.db, 2), 2)

    def test_counter_survives_content_erasure(self):
        """Wiping the owner's content rows must not reset the counter:
        a stale pre-wipe epoch can never be allocated again (the
        RESURRECTED/ABA guard, seam v4.1 G8 + hole 2)."""
        uid = 4242
        stale = owner_counters.allocate_next_epoch(self.db, uid)
        self.db.q("INSERT INTO users(user_id,memory_epoch) VALUES (?,?)",
                  (uid, stale), "none")
        # REPEATED erase/recreate cycles: each cycle wipes the content
        # rows (owner_counters MUST survive), then "recreates" by
        # allocating again. Every allocated value across every cycle is
        # strictly increasing - no stale value is ever reissued.
        seen = [stale]
        for _ in range(10):
            self.db.q("DELETE FROM users WHERE user_id=?", (uid,), "none")
            new = owner_counters.allocate_next_epoch(self.db, uid)
            self.assertGreater(new, seen[-1])
            self.db.q("INSERT INTO users(user_id,memory_epoch)"
                      " VALUES (?,?)", (uid, new), "none")
            seen.append(new)
        self.assertEqual(sorted(set(seen)), seen)

    def test_no_reset_api_exists(self):
        for name in ("reset", "decrement", "set_epoch", "delete_counter"):
            self.assertFalse(hasattr(owner_counters, name),
                             "module must not expose %r" % name)

    def test_concurrent_allocations_are_unique_and_dense(self):
        """N threads x M allocations for one owner: all N*M values
        distinct and exactly 1..N*M (single-statement atomicity).
        NOTE: sqlite thread serialization does NOT prove Postgres
        lock/isolation behavior; it proves the shipped statement is
        atomic and correct under a real SQL engine. PG-specific lock
        semantics are a later gated test under a separate test-schema
        authority."""
        uid = 7
        n_threads, per_thread = 8, 25
        got, lock = [], threading.Lock()
        errors = []

        def worker():
            db = SqliteShimDB(self.path)
            try:
                for _ in range(per_thread):
                    v = owner_counters.allocate_next_epoch(db, uid)
                    with lock:
                        got.append(v)
            except Exception as e:  # pragma: no cover - failure path
                with lock:
                    errors.append(e)
            finally:
                db.conn.close()

        threads = [threading.Thread(target=worker)
                   for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])
        self.assertEqual(sorted(got),
                         list(range(1, n_threads * per_thread + 1)))


class TestGrants(unittest.TestCase):
    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__),
                                 ".pr1_grants_test.sqlite")
        self.db = fresh_db(self.path)

    def tearDown(self):
        self.db.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_generation_starts_zero_and_bumps_monotonically(self):
        uid = 99
        self.assertEqual(grants.get_generation(self.db, uid), 0)
        self.assertEqual(grants.bump_generation(self.db, uid), 1)
        self.assertEqual(grants.bump_generation(self.db, uid), 2)
        self.assertEqual(grants.get_generation(self.db, uid), 2)

    def test_generation_isolated_per_owner(self):
        grants.bump_generation(self.db, 1)
        self.assertEqual(grants.get_generation(self.db, 2), 0)

    def test_grant_round_trip_exact_shape(self):
        uid = 5
        self.db.q(
            "INSERT INTO owner_grants(user_id,scope,granted_at,"
            "expires_at,per_op_expiry_optin) VALUES (?,?,?,?,?)",
            (uid, "memory.write", 1000, None, 1), "none")
        self.db.q(
            "INSERT INTO owner_grants(user_id,scope,granted_at,"
            "expires_at,per_op_expiry_optin) VALUES (?,?,?,?,?)",
            (uid, "memory.erase", 1100, 2000, 0), "none")
        got = {g.scope: g for g in grants.get_grants(self.db, uid)}
        self.assertEqual(set(got), {"memory.write", "memory.erase"})
        w = got["memory.write"]
        self.assertEqual((w.granted_at, w.expires_at,
                          w.per_op_expiry_optin), (1000, None, True))
        e = got["memory.erase"]
        self.assertEqual((e.granted_at, e.expires_at,
                          e.per_op_expiry_optin), (1100, 2000, False))

    def test_grant_active_semantics(self):
        g_open = grants.Grant("s", 1, None, False)
        g_future = grants.Grant("s", 1, 2000, False)
        g_past = grants.Grant("s", 1, 1500, False)
        self.assertTrue(g_open.active(999999))
        self.assertTrue(g_future.active(1999))
        self.assertFalse(g_future.active(2000))  # expiry is exclusive
        self.assertFalse(g_past.active(2000))

    def test_empty_grants_for_unknown_owner(self):
        self.assertEqual(grants.get_grants(self.db, 123456), ())


class TestOwnerIdValidation(unittest.TestCase):
    """Item 2: owner identity must be an exact int; bool/float/str are
    rejected, never coerced (True/1.9/'1' must not alias owner 1)."""

    BAD = (True, False, 1.9, 0.0, "1", "1001", None, b"1", (1,))

    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__),
                                 ".pr1_uid_test.sqlite")
        self.db = fresh_db(self.path)

    def tearDown(self):
        self.db.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_allocate_rejects_non_exact_int(self):
        for bad in self.BAD:
            with self.assertRaises(owner_counters.CounterError,
                                   msg=repr(bad)):
                owner_counters.allocate_next_epoch(self.db, bad)

    def test_grants_reject_non_exact_int(self):
        for bad in self.BAD:
            with self.assertRaises(grants.GrantError, msg=repr(bad)):
                grants.get_grants(self.db, bad)
            with self.assertRaises(grants.GrantError, msg=repr(bad)):
                grants.get_generation(self.db, bad)
            with self.assertRaises(grants.GrantError, msg=repr(bad)):
                grants.bump_generation(self.db, bad)

    def test_exact_int_still_works(self):
        self.assertEqual(owner_counters.allocate_next_epoch(self.db, 1), 1)
        self.assertEqual(grants.get_generation(self.db, 1), 0)


class TestMalformedRowsFailClosed(unittest.TestCase):
    """Item 3: malformed stored rows are rejected with module errors,
    never coerced (bool('false') == True is the canonical trap)."""

    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__),
                                 ".pr1_bad_test.sqlite")
        self.db = fresh_db(self.path)

    def tearDown(self):
        self.db.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def _insert_grant(self, uid=5, scope="memory.write", granted_at=1000,
                      expires_at=None, flag=1):
        self.db.q(
            "INSERT INTO owner_grants(user_id,scope,granted_at,"
            "expires_at,per_op_expiry_optin) VALUES (?,?,?,?,?)",
            (uid, scope, granted_at, expires_at, flag), "none")

    def test_string_flag_rejected(self):
        self._insert_grant(flag="false")  # bool("false") would be True
        with self.assertRaises(grants.GrantError):
            grants.get_grants(self.db, 5)

    def test_empty_scope_rejected(self):
        # mirrored CHECK blocks INSERT; python layer proven via FakeDB
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_grant(scope="")
        class FakeDB:
            def q(self, sql, params=(), mode="all"):
                return [{"scope": "", "granted_at": 1000,
                         "expires_at": None, "per_op_expiry_optin": 0}]
        with self.assertRaises(grants.GrantError):
            grants.get_grants(FakeDB(), 5)

    def test_non_int_timestamps_rejected(self):
        # sqlite column affinity converts '1000' to int on INSERT, so a
        # numeric string is not a malformed-row case; a genuinely
        # non-numeric TEXT value is stored as TEXT and must be rejected
        self._insert_grant(granted_at="abc")
        with self.assertRaises(grants.GrantError):
            grants.get_grants(self.db, 5)
        self.db.conn.close()
        self.db = fresh_db(self.path)
        self._insert_grant(expires_at=1.5)
        with self.assertRaises(grants.GrantError):
            grants.get_grants(self.db, 5)

    def test_whitespace_only_scope_rejected(self):
        # mirrored CHECK blocks INSERT; python layer proven via FakeDB
        with self.assertRaises(sqlite3.IntegrityError):
            self._insert_grant(scope="   ")
        class FakeDB:
            def q(self, sql, params=(), mode="all"):
                return [{"scope": "   ", "granted_at": 1000,
                         "expires_at": None, "per_op_expiry_optin": 0}]
        with self.assertRaises(grants.GrantError):
            grants.get_grants(FakeDB(), 5)

    def test_constraint_blocks_whitespace_and_bad_rows(self):
        # the mirrored CHECK constraints must refuse these at the SQL
        # layer itself (defense below the python validators)
        for bad_sql, params in (
            ("INSERT INTO owner_grants(user_id,scope,granted_at,"
             "expires_at,per_op_expiry_optin) VALUES (?,?,?,?,?)",
             (5, "   ", 1000, None, 0)),
            ("INSERT INTO owner_grants(user_id,scope,granted_at,"
             "expires_at,per_op_expiry_optin) VALUES (?,?,?,?,?)",
             (5, "memory.write", -1, None, 0)),
            ("INSERT INTO owner_counters(user_id,next_epoch)"
             " VALUES (?,?)", (5, 0)),
            ("INSERT INTO grant_generations(user_id,generation)"
             " VALUES (?,?)", (5, -1)),
        ):
            with self.assertRaises(sqlite3.IntegrityError,
                                   msg=str(params)):
                self.db.q(bad_sql, params, "none")

    def test_valid_zero_one_flags_accepted(self):
        self._insert_grant(uid=5, flag=0)
        self._insert_grant(uid=6, flag=1)
        self.assertFalse(grants.get_grants(self.db, 5)[0]
                         .per_op_expiry_optin)
        self.assertTrue(grants.get_grants(self.db, 6)[0]
                        .per_op_expiry_optin)


class TestMalformedShapes(unittest.TestCase):
    """Item 2 (round 2): malformed readback SHAPES must raise
    GrantError/CounterError - never a raw KeyError/TypeError. A fake
    q() returns the wrong shapes directly; no DB involved."""

    class FakeDB:
        def __init__(self, result):
            self.result = result

        def q(self, sql, params=(), mode="all"):
            return self.result

    def _assert_module_error(self, result, fn, err):
        with self.assertRaises(err, msg=repr(result)):
            fn(self.FakeDB(result), 5)

    def test_get_grants_shape(self):
        for bad in ([{}], [{"scope": "memory.write"}], [1], ["x"]):
            self._assert_module_error(bad, grants.get_grants,
                                      grants.GrantError)

    def test_get_generation_shape(self):
        for bad in ({}, 1, "x", {"other": 1}):
            self._assert_module_error(bad, grants.get_generation,
                                      grants.GrantError)

    def test_bump_generation_shape(self):
        for bad in ({}, 1, {"other": 1}):
            self._assert_module_error(bad, grants.bump_generation,
                                      grants.GrantError)

    def test_allocate_shape(self):
        for bad in ({}, 1, "x", {"other": 1}):
            self._assert_module_error(bad,
                                      owner_counters.allocate_next_epoch,
                                      owner_counters.CounterError)


class TestTransactionAtomicity(unittest.TestCase):
    """Item 4: allocation and the caller's content mutation must fail
    TOGETHER; grant change and generation bump must fail TOGETHER.
    Local sqlite BEGIN/ROLLBACK models the caller-owned transaction;
    PG lock/isolation proof stays a later gated test."""

    def setUp(self):
        self.path = os.path.join(os.path.dirname(__file__),
                                 ".pr1_tx_test.sqlite")
        self.db = fresh_db(self.path)

    def tearDown(self):
        self.db.conn.close()
        if os.path.exists(self.path):
            os.remove(self.path)

    def test_allocation_rolls_back_with_content_failure(self):
        uid = 77
        committed = owner_counters.allocate_next_epoch(self.db, uid)
        self.db.begin()
        pending = owner_counters.allocate_next_epoch(self.db, uid)
        self.assertEqual(pending, committed + 1)
        self.db.q("INSERT INTO users(user_id,memory_epoch)"
                  " VALUES (?,?)", (uid, pending), "none")
        # simulated downstream failure: the whole transaction aborts
        self.db.rollback()
        row = self.db.q("SELECT next_epoch FROM owner_counters"
                        " WHERE user_id=?", (uid,), "one")
        self.assertEqual(row["next_epoch"], committed)
        self.assertIsNone(self.db.q(
            "SELECT user_id FROM users WHERE user_id=?", (uid,), "one"))
        # the rolled-back value is simply allocated again later
        self.assertEqual(owner_counters.allocate_next_epoch(self.db, uid),
                         committed + 1)

    def test_grant_change_and_bump_roll_back_together(self):
        uid = 88
        self.db.begin()
        self.db.q(
            "INSERT INTO owner_grants(user_id,scope,granted_at,"
            "expires_at,per_op_expiry_optin) VALUES (?,?,?,?,?)",
            (uid, "memory.write", 1000, None, 0), "none")
        bumped = grants.bump_generation(self.db, uid)
        self.assertEqual(bumped, 1)
        self.db.rollback()
        self.assertEqual(grants.get_generation(self.db, uid), 0)
        self.assertEqual(grants.get_grants(self.db, uid), ())

    def test_committed_bump_survives(self):
        uid = 89
        self.db.begin()
        grants.bump_generation(self.db, uid)
        self.db.commit()
        self.assertEqual(grants.get_generation(self.db, uid), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
