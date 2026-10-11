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


class GrantError(Exception):
    """Malformed input or stored grant row; fail closed."""


def _require_owner_uid(uid: int) -> int:
    """Owner identity must be an EXACT int - bool/float/str are
    rejected, never coerced (same cross-owner aliasing risk as
    owner_counters)."""
    if isinstance(uid, bool) or not isinstance(uid, int):
        raise GrantError(
            "owner id must be an exact int, got %r" % (uid,))
    return uid


def _require_int(value, what: str, minimum: int) -> int:
    """Stored/readback integers must be real ints (never bool) and
    >= minimum; anything else is a malformed row, not a value."""
    if isinstance(value, bool) or not isinstance(value, int)             or value < minimum:
        raise GrantError("invalid %s: %r" % (what, value))
    return value


def _require_flag(value, what: str) -> bool:
    """Stored booleans must be real bools or the integers 0/1. A
    stored string like 'false' would silently coerce True via
    bool(value) - that is exactly the failure this rejects."""
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return value == 1
    raise GrantError("invalid %s: %r" % (what, value))


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
    rows = db.q(GRANTS_READ_SQL, (_require_owner_uid(uid),), "all") or []
    grants_out = []
    for r in rows:
        scope = r["scope"]
        if not isinstance(scope, str) or scope == "":
            raise GrantError("invalid grant scope: %r" % (scope,))
        grants_out.append(Grant(
            scope=scope,
            granted_at=_require_int(r["granted_at"], "granted_at", 0),
            expires_at=(None if r["expires_at"] is None
                        else _require_int(r["expires_at"],
                                          "expires_at", 0)),
            per_op_expiry_optin=_require_flag(
                r["per_op_expiry_optin"], "per_op_expiry_optin"),
        ))
    return tuple(grants_out)


def get_generation(db, uid: int) -> int:
    """Current grants generation for the owner; 0 when no grant change
    has ever happened (no row)."""
    row = db.q(GENERATION_READ_SQL, (_require_owner_uid(uid),), "one")
    return 0 if row is None else _require_int(row["generation"],
                                              "generation", 0)


def bump_generation(db, uid: int) -> int:
    """Bump and return the owner's grants generation. Called by any
    grant add/revoke/scope change INSIDE the same locked transaction
    as the grant-row write it accompanies (v4.1 sec 4 step 2)."""
    row = db.q(GENERATION_BUMP_SQL, (_require_owner_uid(uid),), "one")
    if row is None or "generation" not in row:
        raise GrantError("grant generation bump returned no row")
    return _require_int(row["generation"], "bumped generation", 1)
