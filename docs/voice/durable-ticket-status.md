# Durable ticket-store preparation, NOT runtime activation

cr_voice_tickets.py prepares PostgreSQL ticket issue/redeem/revalidate/revoke with canonical session/conversation validation, exact origin, server time, hash-only ticket/lease storage, row locks and owner/session+IP advisory locks. Explicit persisted consent required; canonical feature flag off by default. Adapter credentials rotated via server-only config, action scoped. No provider or model calls.

Not mounted. Schema not migrated on live DB. Tests mock SQL contracts, NOT actual concurrent PostgreSQL/transport acceptance. DB integration/race/rollback tests remain required before mounting. Existing synthetic VoiceState is not used as production storage.

Missing: progress/final atomic canonical-history transaction and300s recovery, cleanup and erasure hook integration, revocation push, bounded redeem/IP quotas, authenticated HTTP routes and ASGI websocket transport, grant recording/disclosure UX, provider/device/codec tests and acceptance. No production/live readiness claimed.
