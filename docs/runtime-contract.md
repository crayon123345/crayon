# Integrated voice contract V2

Acceptance proposal for both-side review. No live endpoint, provider consent or paid tier granted by this document.

## Ownership and routes
Counterpart stages adapters/dependencies/tests under /ports/voice. Our side owns /integrated canonical UI, authenticated engine and final routing. ASGI gateway required for websocket upgrades; stdlib HTTP gateway does not support them. Live stays unavailable until ASGI route and acceptance pass.
Proposed routes: POST /voice/session, websocket /voice/live, POST /voice/transcribe, POST /voice/speak. Server enforces authentication, exact origin, byte/duration/rate caps and provider quota. No server/provider credentials reach the browser.

## Identity and authentication
Only canonical private engine authenticates accounts, mints and atomically redeems tickets. No arbitrary browser/adapter user_id accepted.
Adapter has separate rotated server-only credential per adapter/environment, restricted to ticket redemption, progress reporting and owner-bound final-result submission. It cannot issue tickets, retrieve account data or call private tools. Rotation/revocation and scope enforced by canonical engine.
POST /voice/session issues opaque one-use ticket bound to owner, canonical session, conversation, exact origin and voice mode; only ticket hash stored. Ticket never placed in URL/query/logs. Redeem in first websocket frame; no provider work before validation. Ticket valid120 seconds from server issuance, rejected at expires_at exactly. Client clock cannot extend it; unavailable clock/auth fails closed.120 seconds allows ordinary mobile microphone permission latency without allowing replay. Concurrent redemption permits one success.
Issuance cap6 per minute per owner/session plus bounded IP backstop; excess 429without provider work.

## Origins and revocation
Exact allowlist of confirmed deployed HTTPS origins. Null, absent, forged and untested native origins rejected. UA never authenticates. No native exception or UA fallback. Bubblewrap TWA expected to use HTTPS, but Android Chrome/TWA headers and roundtrip must be tested across supported OS versions.
Validate canonical session on connect and every15 seconds while active; use push revocation when available. Close by next check; deny effect/result submission immediately after revocation.

## Stream and recovery
Version1 envelope: {version:1,request_id,conversation_id,seq,type,state,text?,error_code?}. IDs server-bound; seq monotonic per request. Types: accepted, progress, transcript_partial, transcript_final, answer_partial, answer_final, review_required, artifact, blocked, done. State: queued,running,awaiting_review,blocked,done. Progress not proof of completion. Provider accepted, content verified and delivery seen separate.
Each call ends at10 minutes OR20 MB total incoming audio, whichever comes first. No guaranteed ten-minute duration for all codecs. Enforce incrementally while streaming. At16 kHz/16-bit mono PCM,10 minutes is19.2 MB before framing; actual codec/framing/device overhead must be tested. Limit rejection returns clear reason; no silent continuation or paid upgrade.
Canonical owner/session/conversation-bound recovery buffer TTL5 minutes stores final text/progress only, no raw audio. Recovery reads only, no provider audio/external-action replay. Final result unique on owner+request_id, atomic insert/compare; conflicting duplicate rejected. Reviewed-effect receipts remain in canonical action store independently of short recovery TTL.

## Storage, privacy and provider use
Our infrastructure keeps raw audio/partial transcript ephemeral, deleting processing buffer after turn or abort. Final transcript/answer uses canonical owner/conversation history with existing retention/deletion controls, not public-engine cache or duplicate adapter history. Recording state and Stop visible; start only on explicit mic/voice action. Audio reply only on current explicit voice request/mode.
These guarantees cover OUR infrastructure only. Gemini free-tier pricing states data is used to improve Google's products. No provider non-retention/no-training promise. Live DISABLED until provider disclosure is shown and user explicitly chooses affected voice mode. No paid key/tier selected. Connected Google/private account content excluded from voice-model path. Recheck provider policy before enablement. Source: https://ai.google.dev/gemini-api/docs/pricing

## Reviewed effects
Voice follows canonical private-engine tool/review gate. Send/Create/update pending until exact account/recipient/content/effect displayed and confirmed through canonical one-use review. Spoken yes cannot approve unseen review, changed draft or new audience. Public lookup/model/tool content grants no permission. Google data stays existing non-model path; no public Gmail write executor.

## Acceptance before enablement
Tests: forged/cross-owner/cross-conversation/expired/replayed tickets; concurrent redemption race; query-ticket rejection; wrong/cross/null/absent origins; issue/redeem rate limits; expiry boundary and skewed client clocks; session revocation midstream; size/duration caps; provider 429/5xx/disconnect; five-minute recovery expiry; atomic final uniqueness; no replay of external writes/audio; transcript retention/deletion; mic denial/stop; STT/TTS quota failure; Android Chrome/TWA origins/header+roundtrip across supported OS versions; real-device voice roundtrip. Provider-effect tests need approved test accounts/content/recipients. Free tiers only. Each /ports adapter supplies deps, boundaries, tests and evidence. Both sides review this single contract before integration.
