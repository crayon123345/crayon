"""Monotone per-owner epoch allocation - memory seam primitive (PR-1).

Design rules (memory seam prep v4.1 sec 4 step 4):
- ONE durable, non-content, monotone per-owner source for ALL
  memory_epoch-setting events: account creation, erasure advancement,
  opt-out purge advancement, and merge advancement. users.memory_epoch
  is only ever SET to an allocated value, never incremented in place
  and never reset by row recreation.
- The counter row SURVIVES erasure of the owner's content rows
  (users/facts/history), so a stale captured epoch can never match a
  later generation: that is the RESURRECTED/ABA guard. There is no
  reset or decrement API in this module by construction.
- allocate_next_epoch() MUST be called inside the caller's locked
  transaction (the owner advisory lock and the footprint table locks
  are already held by the caller per v4.1 sec 4 steps 1-1a). This
  module never opens a transaction and never commits by itself.
- Storage is injected: `db` is any object exposing the integrated
  q(sql, params, mode) contract returning dict rows. No ambient
  connection, no cross-owner path: every function takes `uid`
  explicitly and the SQL filters on it.
"""


class CounterError(Exception):
    """Allocation failed; the caller's transaction must roll back."""


ALLOCATE_SQL = (
    "INSERT INTO owner_counters(user_id,next_epoch) VALUES (%s,1) "
    "ON CONFLICT (user_id) DO UPDATE "
    "SET next_epoch = owner_counters.next_epoch + 1 "
    "RETURNING next_epoch"
)


def allocate_next_epoch(db, uid: int) -> int:
    """Allocate and return the owner's next monotone epoch value.

    First allocation for an owner returns 1; every later allocation
    returns a strictly larger value. Values are never reused for the
    same owner, across account creation, erasure, opt-out purge, and
    merge events alike, because all of them allocate from this one
    source. Caller MUST hold the owner advisory lock inside an open
    transaction (v4.1 sec 4).
    """
    row = db.q(ALLOCATE_SQL, (int(uid),), "one")
    if row is None or "next_epoch" not in row:
        raise CounterError("epoch allocation returned no row for owner")
    value = int(row["next_epoch"])
    if value < 1:
        raise CounterError("epoch allocation returned a non-positive value")
    return value
