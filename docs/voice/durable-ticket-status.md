# Durable ticket-store preparation, NOT runtime activation

cr_voice_tickets.py prepares PostgreSQL ticket issue/redeem/revalidate/revoke with canonical session/conversation validation, exact origin, server time, hash-only ticket/lease storage, row locks and owner/session+IP advisory locks. Explicit persisted consent required; canonical feature flag off by default. Adapter credentials rotated via server-only config, action scoped. No provider or model calls.

Not mounted. Schema not migrated on live DB. Twelve unit tests mock SQL contracts. Dedicated scratch PostgreSQL14 UTF8 smoke verified8-threadredeem exactly1winner,12-threadissue exactly6allowed, expiry_at=now rejected, revoke denied, lease-insert failure rolled back ticket claim, scoped erasure empty. verify_voice_tickets_postgres.py is opt-in destructive local EMPTY scratch only. No live DB or HTTP/ASGI/provider/device acceptance. Wider deployment/session revocation races and retention tests remain required before mounting. Existing synthetic VoiceState is not used as production storage.

Missing: progress/final atomic canonical-history transaction and300s recovery, cleanup and erasure hook integration, revocation push, bounded redeem/IP quotas, authenticated HTTP routes and ASGI websocket transport, grant recording/disclosure UX, provider/device/codec tests and acceptance. No production/live readiness claimed.
