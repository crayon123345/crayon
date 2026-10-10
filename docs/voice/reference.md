# Canonical voice adapter API v1 - reference contract

Revision2: consistent component event enums across reference/OpenAPI/handler. Interface for review and synthetic adapter work. Not deployed. Does not activate Live. The accepted runtime-contract.md remains unchanged; this specifies its API shapes.

## Boundaries

Canonical owner is derived from authenticated web session, never an adapter/body user ID. Adapter uses a separate server-only rotated credential, scoped to redeem/revalidate/progress/final only. No ticket issue, account reads or private tools. Do not put tickets/credentials in URLs or logs. TLS required on deployed routes.

Origin attestation means checking the browser's exact HTTPS Origin header against the configured deployment origin AND the canonical ticket's bound origin. It is not native-device attestation or a cryptographic proof of the browser. Adapter must read actual websocket Origin, never a client message/UA. Canonical trusts the scoped adapter to report that observed header on redeem. Missing/null origin rejected, no UA/native exception. A malicious holder of the adapter credential is outside this header guarantee; rotate/revoke the credential and do not expose it to clients. Android Chrome/TWA actual headers/roundtrip remain required device tests.

## Browser issue

POST /voice/session
Authorization: Bearer <canonical web session>; Origin: <exact HTTPS deployment origin>
Body: {version:1, conversation_id:<owned existing conversation>, mode:transcribe|speak|live, provider_consent_version:<accepted displayed disclosure version>}
Response 201: {version:1,ticket:<opaque once ticket>,request_id,conversation_id,mode,expires_at,expires_in:120,audio_limits:{max_seconds:600,max_bytes:20000000},revalidate_seconds:15,recovery_seconds:300}

Owner/session/conversation/origin/mode bound server-side. Store ticket HASH only. Deny modes without current explicit consent/disclosure, live feature flag off by default. Six issues/minute owner+session plus bounded IP backstop. Exactly expires_at is expired. No provider call on issue.

## Adapter redeem

POST /voice/internal/redeem
Authorization: Bearer <scoped adapter credential>
Body: {version:1,ticket,observed_origin,mode}
Response 200: {version:1,lease_id,request_id,conversation_id,mode,origin,expires_at,revalidate_seconds:15,audio_limits:{max_seconds:600,max_bytes:20000000}}

Redeem ticket from first websocket frame only. Query tickets rejected. Atomic pending->redeemed update where valid session, ticket nonexpired, mode/origin exact. Only one concurrent redeem succeeds. Lease ID opaque; owner ID and account data not disclosed to adapter. Start provider work only after successful response.

## Revalidate and revoke

POST /voice/internal/revalidate
Authorization: scoped adapter credential
Body: {version:1,lease_id}
Response 200: {version:1,active:true,request_id,expires_at,next_check_seconds:15}
Response 410: {error_code:revoked|expired}

Adapter checks immediately before provider connect and every15 seconds while active; closes on 410 or unavailable validation. No grace fallback. Canonical rechecks live session on ALL progress/final calls, denying immediately after revocation, not merely after15 seconds. Duration from canonical redeemed_at, bytes enforced incrementally by gateway and adapter, first cap ends turn. No automatic continuation.

POST /voice/revoke
Canonical session+exact Origin. Body {version:1,request_id}. Owned request only, response {revoked:true}. Also revoke on canonical logout/session revoke. Canonical gateway pushes {version:1,type:blocked,state:blocked,error_code:revoked,request_id,conversation_id,seq} when connection exists; periodic revalidate is fallback. Adapter never gets a session token.

## Progress/final

POST /voice/internal/progress
Scoped credential. Body {version:1,lease_id,seq,type,state,text?}
Response {accepted:true,seq}. seq monotonic per request. Types accepted/progress/transcript_partial/transcript_final/answer_partial/answer_final/review_required/artifact/blocked/done. transcript_final and answer_final are component events, not terminal result commits; they carry verified final component text and can enter the short owner-bound recovery buffer. done on progress is state notification only, never final uniqueness/history completion. No email/private-account content in voice provider path. Progress alone is not terminal completion or proof of an external effect. Partials ephemeral; only final component text/progress may enter recovery. Terminal final submission commits the combined result once.

POST /voice/internal/final
Scoped credential. Body {version:1,lease_id,request_id,seq,type:done,transcript_text?,answer_text?,state:done|blocked,error_code?}
Response201 {accepted:true,request_id,seq}; exact repeated payload200 {accepted:true,duplicate:true}; conflicting repeated payload409 {error_code:final_conflict}. Key owner+request_id; one terminal final envelope containing transcript_text/answer_text as applicable, payload hash atomic uniqueness. Intermediate transcript_final is a progress envelope, not a second terminal final. request_id and conversation_id recovered from lease, reject mismatch. Canonical history written only after validated final transcript/answer processing; adapter cannot invent an effect receipt. Voice effects always require existing exact canonical review, not spoken yes. No direct adapter action execution.

## Read-only recovery

GET /voice/result?request_id=<non-secret request id>
Canonical session and exact Origin. Returns {version:1,request_id,conversation_id,state,events:[final text/progress envelopes],expires_at}. Owner+current-session+conversation binding, available300 seconds from final receipt (not extended by reads). No raw audio, no partial transcript, no action/provider/audio replay. Expired410. External reviewed-effect receipts remain canonical action store independently.

## Errors

400 malformed/version/mode/body;401 unauthenticated;403 origin/consent/credential scope;404 unknown/not-owned (no existence leak);409 already_redeemed/sequence_conflict/final_conflict;410 expired/revoked;413 audio_size;429 rate_limit;503 auth_clock_or_provider_unavailable. Fail closed before provider work/effects. Client clock never controls expiry. No paid upgrade/retry implied.

## Synthetic acceptance

Issue/redeem race, exact expiry, mode/origin mismatch/null/absent, wrong owner/session/conversation, adapter revocation, issue/redeem limits, revalidate failure/logout, cap boundary, monotonic seq/final uniqueness/recovery TTL, no query tickets/provider calls before redeem/no write replay. Device/provider disclosure/real roundtrip tests remain separate activation gates. Implementations must reject extra identity/tool fields.
