# /ports/voice - synthetic staging adapter (REV3)

> STATUS: INCOMPLETE DISABLED SCAFFOLD. Transport-injected, unmounted, no
> routes, no provider calls, no activation. Remaining obligations before
> any activation consideration: enforceable provider/work cancellation
> (process isolation or provider-side cancel API - threads cannot be
> force-killed, so there is NO post-close-effect guarantee for
> noncooperative work), durable stores, canonical session validation,
> scoped credential rotation, rate quotas, feature flags, consent display,
> ASGI websocket gateway, revocation push, erasure, real
> browser/device/codec/provider roundtrips.

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
  the worker is started at all (a denied startup means zero work effects).
  Cancellation is COOPERATIVE ONLY: on close the worker's threading.Event
  is set and it is joined with a bounded timeout; a worker that ignores
  cancel and outlives the join is DETECTED (is_alive) and surfaced as
  `worker_survived_cancel` (original denial chained), and the adapter
  marks itself permanently UNCLEAN and refuses all further work - but
  post-close effects from the survivor are UNBOUNDED. There is no
  no-after-close-effects guarantee for noncooperative work. Enforceable
  cancellation (process isolation) is listed as future work above.
  ALL incoming timing values are clamped to contract bounds:
  `revalidate_seconds` can tighten but never widen past the 15s cadence
  (a 3600s canonical value is clamped; non-positive falls back to 15),
  which also bounds the cancellation join timeout.
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

`pytest ports/voice` - 46 synthetic tests, including a genuine threaded
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
rejection, and server-cap clamping to contract bounds. REV5/REV6 rework coverage (effect-recording fakes): denied startup
produces ZERO work effects (worker never invoked); revocation during
blocked COOPERATIVE work sets cancel, joins the worker inside run_turn,
and asserts no effect after close; success path joins the worker cleanly;
a NONCOOPERATIVE worker that ignores cancel and outlives the bounded join
is detected (worker_survived_cancel, denial chained) and the adapter then
refuses redeem/revalidate/drive/bytes/progress/final/provider-guard as
adapter_unclean; allowlist is mandatory (absent/empty construction
refuses, no module-level default exists); canonical revalidate_seconds of
3600 and 0 are both clamped to the 15s contract cadence; a revalidate
response OMITTING lease_id is rejected as non-conformant bad_binding
(coordinated canonical+adapter spec: lease_id echo required).
