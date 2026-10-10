# Change log

## October 10, 2026 - Workspace review and public GitHub entry points

- Telegram update-sheet command shows account/file/range/before/RAW values and exact expiring one-use confirmation, rejects account switch and wrong-user/chat reviews. Web uses existing session-bound Sheet review.
- Web public GitHub digests and identity-only connection entry point; private repository access remains disabled. Live public itsppm76/crayon digest returned verified metadata/commit URLs.
- Docs text/Sheets read paths retain separate own-account Google Workspace consent, Testing limits and no model-memory path. No real Workspace write performed.
-367tests; full provider consent remains separate from deployed code.


## October 10, 2026 - web computer, queue and private notifications

- Enable authenticated web computer/browser and /work internal-queue controls with existing per-account quotas and private file folders.
- Standalone reminders/read-only scheduled results and work completion save encrypted private in-app notifications, deduplicated by source, with account-bound Mark read. No invented Telegram destination or OS push while closed.
- Add Menu entries, readable mobile notification dialog and remove the disabled consolidation entry from the visible menu.
-364tests; disposable Postgres account/dedup/acknowledgement proof and390px/1280px visual checks. Full parity/group rooms/deletion remain pending.


## October 9, 2026 - per-user access

- Open bounded Telegram computer/browser and controlled public-form previews to all authenticated users. Preserve free per-user/global caps and exact effect review.
- Bind jobs to authenticated account IDs. Private file folders isolate each account, preserve legacy owner files, reject old-worker file jobs, symlinks and overwrites;100files/account,10MB shared cap.
- Calendar read/preview/reconnect/Create and opt-in hourly mail-watch route to each user's own encrypted Google connection, never the owner's account. Exact review and Google Testing constraints remain. Mail-watch delivers only to its opting-in private Telegram account.
-360 local tests and disposable PostgreSQL queue/private-file isolation proof. Real arbitrary-user Google consent, Calendar Create and Telegram acceptance remain separate gates.
- No consolidation, paid budget increase or automatic external action.


## October 9, 2026 - final live-state documentation

- Document cumulative Day 0-to-current features in `LIVE_FEATURE_CHECKLIST.md`.
- Record deployed runtime `e4331d3`,357 passing committed tests, healthy database/polling and configured Telegram/Google/email login.
- Google-first and verified email/password sign-in are deployed, with provider-independent IDs and no email auto-match. Existing linked identities keep their account. Real standalone sign-in/reset acceptance remains unclaimed.
- Account consolidation stays OFF by owner choice; no production merge. Scoped acceptance/two-Google preparation was stopped and never deployed.
- Separate Telegram/general features from web availability, named-tester/owner beta and setup-dependent Workspace/GitHub paths. Keep browser-only reminders/background alerts and web parity gaps explicit.
- This update changes documentation only. It does not enable a flag, send mail, book anything or move data.

## October 9, 2026 - deployed identity and inspection builds

- `f3cda8c`: isolated verified email/password auth with current revocation/disabled checks, Google-first standalone accounts, explicit unused-identity linking and provider removal review.
- `e4331d3`: exact-reviewed standalone-to-Telegram consolidation implementation published for inspection behind an OFF flag. Live bridge rejects consolidation before provider login. No production merge or real two-login test occurred.


## October 9, 2026 - auth callback recovery

- Ignore inert Google callback metadata while keeping unique code/state, PKCE, cookie and signed identity checks.
- Friendly return-to-Crayon pages on provider navigation errors; original-tab notification stops polling.
- Failure-category logs contain no OAuth values. Regression tests cover metadata, duplicate/missing/blank state and script escaping.
- Version both frontend CSS and JS to avoid mixing old styles with new login markup.

## October 9, 2026 - web release and recovery

- GitHub Pages frontend and Telegram OIDC identity mapping to existing member records.
- Seven-day server sessions, same-browser persistence and explicit logout/expiry.
- Recorded history pagination, visible-tab refresh and same-user Telegram mirrors for ordinary chats/upload analysis.
- Encrypted generic command receipts, kept outside model memory. No historical-body reconstruction.
- Uploads up to 20 MB, temporary raw bytes, saved analysis and generated-file activity.
- Exact, session-bound one-use review for web email/calendar/RAW Sheet writes. Private Google reads bypass AI/history.
- Typing bubble and 12/30/60-second honest waiting updates, cleared on result/error/logout.
- Lost POST acknowledgements/result-fetch failures recover by safe GET checks of the original request ID. No automatic resubmission.
- Allowed-origin CORS on upload authentication errors.
- Saved-session reconnect after transient failures and versioned frontend script caching.
- 317 local tests passed; captured desktop/mobile checks. Owner-device post-fix recovery acceptance pending.

See [release checklist](WEB_RELEASE_CHECKLIST.md) for remaining parity work and deliberately locked features. No paid tier/payment setup, no acceptance-test email/calendar/Sheet write.

## Google login addition (October 9, 21:22 IST)

- Dedicated Google OIDC client with basic identity scopes, state/cookie/nonce/S256 PKCE, signed token checks.
- Explicit session-bound review links stable Google subject to existing Telegram UID; no email matching, account merge or silent integration permissions.
- Unlinked users start with Telegram and link once; Google external Testing restrictions remain.
- 326 local tests passed. Dedicated login configuration and backend/Pages rollout verified; real Google account picker reached. Callback permits inert Google-returned metadata without trusting it.
- Cloud Audience confirmed external Testing with existing testers. Real owner exact link and subsequent Google login returning the same UID/history remain pending. No identity linked or provider data-access granted by this rollout.

- Scope correction: Google documents an allowlist exception for basic identity-only login in Testing. Earlier blanket named-tester login wording was too broad; Gmail/Calendar data-access tester limits are separate. See https://developers.google.com/identity/protocols/oauth2/production-readiness/overview .

## Isolated public Google login (October 9, 21:36 IST)

- Owner picked separate identity-only production project. Existing Gmail/Calendar Testing project unchanged.
- New login project published; only openid/email/profile declared, no sensitive/restricted verification required. Branding is unverified, not a certified-app claim.
- Existing login behavior is still Telegram-first and explicitly reviewed linking. Google/email-first accounts and later reviewed merge are new requested work, not shipped here.
