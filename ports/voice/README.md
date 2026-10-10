# /ports/voice - synthetic staging adapter (REV3)

Adapter-side client for the canonical voice API v1 (docs/voice, REV3).
STAGING ONLY: transport-injected, no routes, no provider calls, no device,
no activation. The accepted runtime-contract.md is unchanged.

## What this implements (adapter obligations)

- Fail closed: no provider work before a successful redeem
  (`provider_connect_guard` = revalidate immediately before connect).
- Revalidate every 15s while active; close on 410/unavailable. No grace.
- Ticket only in the redeem request body, never in a URL (test-enforced).
- Monotonic seq per request; component events vs one terminal /final envelope.
- Ruling (b): /final denied after lease/session expiry or revocation means
  commit status UNKNOWN - stop, report unknown, never replay provider work or
  effects; resolution only via the authorized browser read-only /voice/result
  within the original 300s TTL.

## Not here (activation gates, per docs/voice/implementation-status.md)

Durable stores, canonical session validation, scoped credential rotation,
rate quotas, feature flags, consent display, ASGI websocket gateway,
revocation push, erasure, real browser/device/codec/provider roundtrips.

## Tests

`pytest ports/voice` - 14 synthetic tests incl. redeem race, exact expiry,
origin/mode mismatch, immediate session revoke, final duplicate-while-live,
commit-unknown after expiry/revocation, ticket-never-in-URL.
