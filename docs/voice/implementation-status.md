# Implementation status

Contract and synthetic state reference only. NOT production or mounted routes.

The synthetic VoiceState model deliberately accepts owner/session/conversation/adapter arguments from its test caller. It is not authentication, does not validate owned conversations, enforce IP quota/adapter credential scopes, persist canonical history, implement push revocation or evict expired in-memory rows. Never expose it as a route.

Before activation: durable Postgres transactions for ticket claim/lease/sequence/final history and TTL cleanup; actual canonical session + email revoke + conversation validation; rotated scoped adapter authentication; issue/IP/redeem caps; feature flag and explicit current provider consent; HTTP integration and ASGI websocket gateway; progress revocation push; account erasure; real browser/device/codec/provider failures and roundtrip. No provider calls in this PR. No live activation/cutover.

The /ports/voice staging adapter is an INCOMPLETE DISABLED SCAFFOLD: its
run_turn cancellation is cooperative only (threads cannot be force-
killed); a noncooperative worker surviving the bounded join is detected
and surfaced (worker_survived_cancel) and the adapter turns permanently
unclean, but post-close effects from such a survivor are UNBOUNDED.
Enforceable cancellation (process isolation or provider-side cancel) is
required before activation. Incoming timing/cap values are clamped to
contract bounds; the origin allowlist is required configuration with no
defaults.

Eleven synthetic tests cover exact expiry, origin/null/absent mismatch, consent rejection, concurrent one-use claim, mode binding, owner/session rate limit, immediate session revoke, 10min/20MB boundary, monotonic component events, final duplicate/conflict, post-final denial, short owner/session/conversation recovery, adapter mismatch/revoke and lost-final-response retries denied after expiry/request revoke/session revoke (ruling b). They do not prove deployment or transport correctness.
