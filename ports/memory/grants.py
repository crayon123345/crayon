"""Owner grants store skeleton - memory seam primitive (PR-1).

Design rules (memory seam prep v4.1 sec 3-4):
- A Grant is an EXPLICIT owner grant read from the store, never a
  caller-supplied frozenset. Capabilities resolve grants at mint and
  re-read them inside the transaction (v4.1 sec 4 step 2).
- grant_generation is a per-owner monotone counter: any grant add,
  revoke, or scope change bumps it, so a capability minted against an
  older generation fails closed as REVOKED.
- Grant rows carry their own expiry (expires_at, NULL = no expiry) and
  a per-operation expiry opt-in flag; generation bumps and grant row
  writes happen inside the same locked transaction as the change they
  describe. This module never opens a transaction by itself.
- Storage is injected (same q(sql, params, mode) contract as
  owner_counters); every function takes `uid` explicitly.
"""

from dataclasses import dataclass
from typing import Optional, Tuple


@dataclass(frozen=True)
class Grant:
    """One explicit owner grant. Shape per seam prep v4.1 sec 3."""
    scope: str
    granted_at: int
    expires_at: Optional[int]      # None = no expiry
    per_op_expiry_optin: bool

    def active(self, now: int) -> bool:
        """Time validity only; generation checks happen separately."""
        return self.expires_at is None or now < self.expires_at


GRANTS_READ_SQL = (
    "SELECT scope,granted_at,expires_at,per_op_expiry_optin "
    "FROM owner_grants WHERE user_id=%s"
)
GENERATION_READ_SQL = (
    "SELECT generation FROM grant_generations WHERE user_id=%s"
)
GENERATION_BUMP_SQL = (
    "INSERT INTO grant_generations(user_id,generation) VALUES (%s,1) "
    "ON CONFLICT (user_id) DO UPDATE "
    "SET generation = grant_generations.generation + 1 "
    "RETURNING generation"
)


def get_grants(db, uid: int) -> Tuple[Grant, ...]:
    """All stored grant rows for the owner (expired included; callers
    filter with Grant.active(now)). Read at capability mint AND
    re-read inside the transaction (v4.1 sec 4 step 2)."""
    rows = db.q(GRANTS_READ_SQL, (int(uid),), "all") or []
    return tuple(
        Grant(
            scope=str(r["scope"]),
            granted_at=int(r["granted_at"]),
            expires_at=(None if r["expires_at"] is None
                        else int(r["expires_at"])),
            per_op_expiry_optin=bool(r["per_op_expiry_optin"]),
        )
        for r in rows
    )


def get_generation(db, uid: int) -> int:
    """Current grants generation for the owner; 0 when no grant change
    has ever happened (no row)."""
    row = db.q(GENERATION_READ_SQL, (int(uid),), "one")
    return 0 if row is None else int(row["generation"])


def bump_generation(db, uid: int) -> int:
    """Bump and return the owner's grants generation. Called by any
    grant add/revoke/scope change INSIDE the same locked transaction
    as the grant-row write it accompanies (v4.1 sec 4 step 2)."""
    row = db.q(GENERATION_BUMP_SQL, (int(uid),), "one")
    if row is None or "generation" not in row:
        raise ValueError("grant generation bump returned no row")
    return int(row["generation"])
