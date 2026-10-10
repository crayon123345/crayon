# /ports/voice - synthetic staging adapter (REV3)

Adapter-side client for the canonical voice API v1 (docs/voice, REV3).
STAGING ONLY: transport-injected, no routes, no provider calls, no device,
no activation. The accepted runtime-contract.md is unchanged.

## What this implements (adapter obligations)

- Fail closed: no provider work before a successful redeem
  (`provider_connect_guard` = revalidate immediately before connect).
- ACTIVE revalidation cadence: `run_turn`/`drive_once` revalidate every
  `revalidate_seconds` (15, contract-pinned and asserted) while active; ANY
  denial, `active:false`, or a timed-out/unavailable validation closes the
  lease. No grace fallback.
- Incremental caps: elapsed time from redeem and audio bytes
  (`note_audio_bytes`) are tracked per lease; first breach of the
  600s / 20MB caps ends the turn (CapExceeded, lease closed).
- Ticket only in the redeem request body, never in a URL (test-enforced).
- Local binding validation: observed_origin must be a non-null https://
  origin; canonical redeem responses must carry the required fields and
  echo the bound mode/origin; `active:false` on revalidate closes.
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

`pytest ports/voice` - 26 synthetic tests, including a genuine threaded
redeem race (barrier-synchronized, exactly one winner), a real lost-commit
(canonical commits, response transport dies -> CommitUnknown + close + no
replay), exact 120s expiry boundary, incremental 600s/20MB cap enforcement,
terminal-guard duplicate/conflict, revalidate-timeout fail-closed,
active:false binding close, non-HTTPS/null-origin local rejection, and the
15s cadence contract pin.
