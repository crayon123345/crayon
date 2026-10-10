# /ports/voice - synthetic staging adapter (REV3)

Adapter-side client for the canonical voice API v1 (docs/voice, REV3).
STAGING ONLY: transport-injected, no routes, no provider calls, no device,
no activation. The accepted runtime-contract.md is unchanged.

## What this implements (adapter obligations)

- Fail closed: no provider work before a successful redeem
  (`provider_connect_guard` = revalidate immediately before connect).
- ACTIVE revalidation cadence: `run_turn` revalidates every
  `revalidate_seconds` (15, contract-pinned and asserted) of WALL-CLOCK
  time while active - turn work runs on a worker thread, so blocking work
  cannot stretch the cadence; ANY denial, `active:false`, binding mismatch,
  or an unavailable validation closes the lease. No grace fallback.
  Ordinary transport failures (builtin `TimeoutError`/`OSError`) fail
  closed exactly like an explicit `TransportTimeout`, on every path.
  Startup ordering is load-bearing: the FIRST validation completes before
  the worker is started at all (a denied startup means zero work effects),
  and on any close the worker is cooperatively cancelled
  (`work(lease, cancel)` receives a threading.Event) and JOINED before the
  error propagates - no effect lands after close.
- Incremental caps: elapsed time from redeem and audio bytes
  (`note_audio_bytes`) are tracked per lease; first breach of the
  600s / 20MB caps ends the turn (CapExceeded, lease closed). Negative or
  non-numeric byte increments are rejected (never applied). Server-provided
  caps are CLAMPED to the contract bounds - a server may tighten the
  600s / 20MB limits but never widen them.
- Ticket only in the redeem request body, never in a URL (test-enforced).
- Local binding validation: observed_origin must parse as a strict https
  origin (non-empty host; no credentials, path, query or fragment) AND be
  on the adapter's exact-origin allowlist. `allowed_origins` is REQUIRED
  and has NO default - it is the confirmed merged-deployment configuration
  and construction fails closed without it, because a shipped default
  cannot establish origin authority. Canonical redeem responses must carry
  the
  required fields and echo the bound mode/origin; revalidate responses must
  echo the lease's own lease_id, request_id and expires_at (a mismatch
  closes the lease as `bad_binding`); `active:false` on revalidate closes.
- Monotonic seq per request; component events vs one terminal /final.
- Terminal guard: after a final, only an IDENTICAL final retry while the
  lease is live passes (canonical duplicate path); post-final progress and
  conflicting finals are rejected locally.
- Ruling (b): a lost /final transport response or a liveness denial
  (401/403/410) means commit status UNKNOWN - close, report unknown, never
  replay provider work or effects; resolution only via the authorized
  browser read-only /voice/result within the original 300s TTL.

## Not here (activation gates, per docs/voice/implementation-status.md)

Durable stores, canonical session validation, scoped credential rotation,
rate quotas, feature flags, consent display, ASGI websocket gateway,
revocation push, erasure, real browser/device/codec/provider roundtrips.

## Tests

`pytest ports/voice` - 41 synthetic tests, including a genuine threaded
redeem race (barrier-synchronized, exactly one winner), a real lost-commit
(canonical commits, response transport dies -> CommitUnknown + close + no
replay), exact 120s expiry boundary, incremental 600s/20MB cap enforcement,
terminal-guard duplicate/conflict, revalidate-timeout fail-closed,
active:false binding close, non-HTTPS/null-origin local rejection, and the
15s cadence contract pin. REV4 rework regression coverage: builtin
TimeoutError/OSError fail-closed on revalidate/progress/final, strict
origin parsing (empty host, credentials, path, query, fragment, look-alike
host) plus allowlist rejection, revalidate request_id/expires_at binding,
wall-clock cadence under genuinely blocking work (threaded, 15s gap
asserted) and denial-during-blocking-work close, negative byte-increment
rejection, and server-cap clamping to contract bounds. REV5 rework
coverage (effect-recording fakes): denied startup produces ZERO work
effects (worker never invoked); revocation during blocked work sets
cancel, joins the worker inside run_turn, and asserts NO effect after
close; success path joins the worker cleanly; allowlist is mandatory
(absent/empty construction refuses, no module-level default exists).
