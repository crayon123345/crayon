# PR-1 proposed schema - REVIEW DOCUMENT ONLY, NOT A MIGRATION

No DDL from this document is applied anywhere by PR-1. Migration is not
authorized. Table shapes are the app owners' schema decision; this
document proposes them for cross-review so the PR-1 code and tests have
an exact contract to bind to. Pinned team-repo head:
d63801c06c4f3fa93ead7e1d78fc557e9e32b94b.

## owner_counters - monotone per-owner epoch source (v4.1 sec 4 step 4)

```sql
CREATE TABLE owner_counters(
  user_id    BIGINT PRIMARY KEY,
  next_epoch BIGINT NOT NULL CHECK (next_epoch >= 1)
);
```

- Non-content. One row per owner. SURVIVES erasure of the owner's
  users/facts/history rows: wipe/delete paths must NOT delete this row
  (that is what makes stale epochs fail terminally as RESURRECTED).
- Allocated ONLY by ports/owner_counters.py::allocate_next_epoch inside
  the caller's locked transaction. First allocation returns 1.
- users.memory_epoch is only ever SET to an allocated value, never
  incremented in place and never reset by row recreation.

## owner_grants - explicit owner grants (v4.1 sec 3)

```sql
CREATE TABLE owner_grants(
  user_id             BIGINT NOT NULL,
  scope               TEXT   NOT NULL,
  granted_at          BIGINT NOT NULL,
  expires_at          BIGINT,               -- NULL = no expiry
  per_op_expiry_optin BOOLEAN NOT NULL DEFAULT FALSE,
  PRIMARY KEY (user_id, scope),
  CHECK (scope <> ''),
  CHECK (granted_at >= 0),
  CHECK (expires_at IS NULL OR expires_at >= 0)
);
```

- Explicit owner grants only; never caller-assembled. Scope set is
  closed and owner-decided (e.g. memory.write, memory.sensitive,
  memory.erase - exact set is a later schema/scope decision).

## grant_generations - per-owner grant generation (v4.1 sec 4 step 2)

```sql
CREATE TABLE grant_generations(
  user_id    BIGINT PRIMARY KEY,
  generation BIGINT NOT NULL CHECK (generation >= 0)
);
```

- Bumped inside the same locked transaction as every grant add/revoke/
  scope change. A capability minted against an older generation fails
  closed as REVOKED. No row = generation 0.

## Local test schema note

PR-1 tests run against LOCAL disposable SQL only (sqlite shim or an
ephemeral local Postgres container) with a dialect mirror of the
shapes above. No hosted/external database is written anywhere; a real
hosted test-schema run needs a separate approved authority.
