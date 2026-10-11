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

    def q(self, sql, params=(), mode="all"):
        cur = self.conn.execute(sql.replace("%s", "?"), tuple(params))
        if mode == "none":
            return None
        rows = [dict(r) for r in cur.fetchall()]
        if mode == "one":
            return rows[0] if rows else None
        return rows


SCHEMA = (
    "CREATE TABLE owner_counters(user_id INTEGER PRIMARY KEY,"
    " next_epoch INTEGER NOT NULL)",
    "CREATE TABLE owner_grants(user_id INTEGER NOT NULL,"
    " scope TEXT NOT NULL, granted_at INTEGER NOT NULL,"
    " expires_at INTEGER, per_op_expiry_optin INTEGER NOT NULL DEFAULT 0,"
    " PRIMARY KEY (user_id, scope))",
    "CREATE TABLE grant_generations(user_id INTEGER PRIMARY KEY,"
    " generation INTEGER NOT NULL)",
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
        # wipe: content rows go, owner_counters row MUST remain
        self.db.q("DELETE FROM users WHERE user_id=?", (uid,), "none")
        # recreation allocates from the same source: strictly larger,
        # never the stale value
        for _ in range(5):
            new = owner_counters.allocate_next_epoch(self.db, uid)
            self.assertGreater(new, stale)
        final = owner_counters.allocate_next_epoch(self.db, uid)
        self.assertEqual(final, stale + 6)

    def test_no_reset_api_exists(self):
        for name in ("reset", "decrement", "set_epoch", "delete_counter"):
            self.assertFalse(hasattr(owner_counters, name),
                             "module must not expose %r" % name)

    def test_concurrent_allocations_are_unique_and_dense(self):
        """N threads x M allocations for one owner: all N*M values
        distinct and exactly 1..N*M (single-statement atomicity)."""
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
