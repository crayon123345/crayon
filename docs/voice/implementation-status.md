# Implementation status

Contract and synthetic state reference only. NOT production or mounted routes.

The synthetic VoiceState model deliberately accepts owner/session/conversation/adapter arguments from its test caller. It is not authentication, does not validate owned conversations, enforce IP quota/adapter credential scopes, persist canonical history, implement push revocation or evict expired in-memory rows. Never expose it as a route.

Before activation: durable Postgres transactions for ticket claim/lease/sequence/final history and TTL cleanup; actual canonical session + email revoke + conversation validation; rotated scoped adapter authentication; issue/IP/redeem caps; feature flag and explicit current provider consent; HTTP integration and ASGI websocket gateway; progress revocation push; account erasure; real browser/device/codec/provider failures and roundtrip. No provider calls in this PR. No live activation/cutover.

Synthetic tests cover exact expiry, origin/null/absent mismatch, consent rejection, concurrent one-use claim, mode binding, owner/session rate limit, immediate session revoke, 10min/20MB boundary, monotonic component events, final duplicate/conflict, post-final denial, short owner/session/conversation recovery, adapter mismatch/revoke. They do not prove deployment or transport correctness.
