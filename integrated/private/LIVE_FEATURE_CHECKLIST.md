# Crayon: final live feature checklist

October 10, 2026. Release: per-user web/Telegram parity milestones; health version: `2.35.0`.
This is a cumulative Day 0-to-current inventory, not a promise that every feature works on every login or device. This update removes product owner/tester gates only after adding per-account isolation. External provider approval and channel limits remain.

## Accounts and web

- [x] Telegram sign-in opens the existing Telegram account, recorded chat and memory.
- [x] Identity-only Google sign-in can create an independent account without Telegram. An already linked Google identity opens its existing account. Login does not grant Gmail, Calendar or Workspace access.
- [x] Email/password registration, verification, sign-in and password-reset UI are deployed. Verified email and current disabled/revoked-token checks are required. Email identity is separate from Google identity even if the address matches.
- [x] Conventional responsive sign-in screen and readable provider-error recovery pages.
- [x] Exact-reviewed linking of an unused Google/email identity to the signed-in account; no email-based matching or silent history move.
- [x] Seven-day browser sessions, refresh/new-tab persistence, logout/expiry and recovery after temporary network/host failures.
- [x] Recorded chat pagination, visible-tab sync (about 30 seconds), typing bubble and honest waiting updates.
- [x] Ordinary web chat/upload-analysis mirrors to Telegram for Telegram-backed accounts only. Standalone accounts have no Telegram destination.
- [x] Uploads up to 20 MB, media analysis, saved analysis/metadata and generated-file activity. Raw upload bytes are temporary.
- [x] Lost upload/chat acknowledgements recover through the original request ID, not automatic repeat POSTs.
- [x] Menu for tasks, memory, notes, recorded history, activity/files and reviewed connection actions.
- [ ] Populated-account consolidation is OFF by owner choice. Separate accounts keep separate chat and memory. No production merge happened; stopped two-Google/scoped-test changes were not deployed.

## Core assistant, since Day 0

- [x] Conversational chat with persistent per-account history and memory across restarts.
- [x] Memory facts/preferences/projects; review suggestions, explicit forget and data wipe on Telegram. No automatic fact merging.
- [x] Save/list notes and contextual follow-ups.
- [x] Current-time lookup, one-off/recurring reminders, scheduled read-only prompts and cancellation on Telegram.
- [x] Tracked multi-day tasks, subtasks, todo/doing/blocked/done, recorded results, reopening, dashboard, CSV and progress-chart export.
- [x] Goal plans with bounded tool use and per-step results.
- [x] Public web search, bounded page reading and multi-source research/briefs with source receipts. Citations and synthesis can still be wrong.
- [x] Sandboxed Python maths/data checks; bounded Decimal arithmetic and unit conversion.
- [x] Dated reference currency conversion, not a live tradable FX quote or fee-inclusive price.
- [x] CSV and bar-chart PNG generation from supplied/checked data; formula escaping and size limits.
- [x] Photo, PDF, voice/audio, video and text/code analysis; common Office text/cell extraction and archive listings. Office layout/formulas/images are not verified; archives are not executed.
- [x] Review-only message drafts, complete email drafting from supplied context, optional exact wording. No invented completion or automatic third-party message send.
- [x] Opt-in proactive nudges, morning/evening digests, quiet hours and task check-ins on Telegram.
- [x] Reactions, plain-text replies, buttons, natural-language shortcuts, command menu and timed progress messages.
- [x] Public health endpoint and authenticated self-test endpoint.

## Internal work and groups

- [x] Persistent explicit background work queue: public research, bounded briefs/page reads and arithmetic,1-3 steps per job.
- [x] Queue status, Show/Pause/Resume/Cancel/Export controls, TXT reports and CSV source ledgers. Real Telegram arithmetic, completion, Show and TXT export passed.
- [x] Quiet-hour completion deferral; interrupted work blocks rather than silently repeating. Recorded task completion is not independent verification.
- [x] Tagged group public search/news; untagged messages ignored. News is dated index headlines/publishers, not verified full-article reporting.
- [x] Requester-owned group action route with per-member audience opt-in and requester-only confirmations; private ambient history stays out.
- [ ] Real group-action opt-in/confirmation and bot-add welcome acceptance remain pending. Public group news and tag/ignore paths have real checks.

## Connected-service beta

- [x] Separate per-user encrypted Gmail/Calendar OAuth connections, short personal links, status/disconnect and private intent routing. Google data bypasses Gemini and shared model memory.
- [x] Each user's own Gmail (Google currently permits named testers): search/read and bounded task+inbox-attention reports.
- [x] Exact-reviewed Gmail draft Send/Cancel with From/To/CC/BCC/subject/body, expiry, stored-draft hash checks and no uncertain-send retry. Real owner-reviewed Telegram email send passed. No email attachments.
- [x] Each user's own primary-calendar reads and exact preview/Cancel; optional guests/popup reminders are represented in the reviewed payload. No Meet.
- [x] Per-user opt-in hourly mail watch, bounded sender/subject/snippets and quiet hours. Real Telegram two-item notification passed October9 at15:00IST. Not a complete inbox audit.
- [x] Web exact session-bound one-use reviews for email/calendar/RAW Sheet writes; menu controls, not full conversational parity.
- [ ] Actual calendar Create/invitation still needs write-scope reconnect and owner-approved live acceptance. Do not infer it from preview/Cancel.
- [ ] Workspace Docs text reads and Sheets bounded reads/RAW updates have Telegram/web entry points; Google consent is still required in the separate Testing project. Telegram Sheet changes now have exact one-use private review as on web. Public GitHub commit/PR digests need no connection; public itsppm76/crayon digest verified live. Private repo access/Docs editing are not enabled. Real Workspace consent/write acceptance remains pending.
- [ ] Public Google data-access verification remains an external gate. Identity-only login is distinct from named-tester Gmail/Calendar access.

## Computer/browser beta (all authenticated users)

- [x] Every user's computer status, arithmetic and public HTTPS browser screenshots on a bounded Codespace worker.
- [x] Per-account create/read/list text files in separate private directories; no unrestricted shell, overwrites, deletion or access to another account's files. Existing owner files stay in place.100files/account and10MB shared storage cap.
- [x] Automatic cold-start, worker readiness, calculation and idle-stop have real proof. Free quota and host/worker recovery limits remain.
- [x] Public-site screenshots with public-IP checks, sensitive-portal backstop and URL/action log. Python tutorial, Instagram login wall and signed-out YouTube were visually checked.
- [x] Fixed World Bank2024 India/China/US GDP-per-capita research-to-CSV/chart demo with record checks, findings and screenshot. Not arbitrary website automation.
- [x] Exact-reviewed controlled public-form demo available to every account: real owner preview and one Submit produced receipt/screenshot October9 at15:14IST. This is test-only, not a real booking.
- [ ] Arbitrary other users' real Telegram acceptance remains pending. Dynamic Calendly/Google appointment workflows, account login and general bookings are not accepted capabilities.

## Safety and deliberate limits

- [x] Per-account data isolation, encrypted connection/draft storage, exact one-use action reviews, audit records and bounded jobs/tools.
- [x] Secret interception/redaction, verified deletes, honesty guard and untrusted-page handling. These are safeguards, not guarantees.
- [x] No automatic external messages, purchases, paid setup or uncertain-effect retries.
- [x] Standalone reminders/scheduled read-only jobs/work completion now use private in-app Notifications. Results persist until the next app open; no system push, email or phone alert while closed. Telegram-backed reminders still use Telegram.
- [x] Web computer/browser, /work controls, shared public-context rooms, private check-in/digest controls and exact saved-fact removal reviews are enabled. Telegram has matching public groups, private optional updates and named-key forget. Full private group-action parity and all-data web deletion are unfinished.
- [ ] WhatsApp remains test-only: current production setup lists payment-method/business verification requirements. No card or paid setup is allowed; production onboarding stopped. No outbound acceptance claimed.
- [ ] Free hosts sleep and APIs have shared quotas. Reminder/worker timing and uptime are best-effort. Raw historical media/private command bodies cannot be reconstructed.

## Verification ledger

-377 tests pass for the committed runtime; frontend JavaScript syntax passes. Stopped local acceptance-test changes are excluded.
- Live `/health`: database reachable, polling mode, version2.35.0. Live web status: Telegram, Google and email login configured; uploads, rooms, computer, notifications and work queue enabled.
- Consolidation start rejected401 with "Account consolidation is not enabled yet." Last production environment read found no enable flag.
- Real Telegram login/history, web chat persistence and image-analysis storage were checked. Captured desktop/mobile recovery and generated full review layouts were inspected.
- Real successful standalone Google/email registration/login, password-reset delivery, and owner-device post-fix upload/reconnect acceptance were not performed by this audit. Deployed/configured is not the same as real acceptance.
- No web acceptance test sent an email, created a calendar event, wrote a Sheet or merged accounts.

[Web app](https://itsppm76.github.io/crayon/) · [Bot](https://t.me/crayon_v1_bot) · [Repository](https://github.com/itsppm76/crayon)

## Per-user rollout limits

Computer/browser/text-file commands and controlled-form previews are no longer owner/tester gated in Telegram. Computer execution keeps5 jobs/user/day,20for the existing owner,30total including failures; no budget increase. Queue owner ID comes from the authenticated caller, not user-supplied args. Old worker versions cannot handle file jobs. Machine configuration/lifecycle tokens remain private; the worker exposes only bounded operations.

Calendar and mail-watch use each user's own encrypted connection, never a fallback to the owner's account. Hourly mail checks require opt-in from that user's private Telegram chat and deliver only there; no browser-only delivery. Users who connected before calendar access was open must reconnect for their own calendar scopes. Actual provider Create and other users' live consent remain unproven. Gmail already followed each user's own connection. Google Testing still controls who can grant data scopes; product gates do not override Google approval.

## Web parity milestone

Menu now includes Work queue, Computer and Notifications. `/work list`, explicit bounded research/arithmetic plans and Show/Pause/Resume/Cancel/Export work in authenticated web chat. Public `/browse` and bounded `/computer` actions use the same private owner ID and quotas as Telegram. Standalone reminders and scheduled read-only results go to encrypted, account-bound Notifications, never an invented Telegram number. Notifications deduplicate on source receipt, check account existence and support account-bound Mark read. No OS push is promised. Local desktop/mobile UI and disposable database isolation checked; actual user acceptance remains separate.

## Shared audience release
Web Menu > Shared rooms has explicit create/invite review/join, membership-bound shared messages, leave and owner-confirmed room deletion. Public-context answers use the same isolated answerer as Telegram mentions. Each account can join 10 rooms, each room up to 20 members; 30 messages/account/day and newest 500 messages/room retained (50 shown). Member labels are room-scoped pseudonyms, not account IDs. All current and future members see retained messages; leaving does not delete those messages. No private memory, files or connected Google accounts are imported.
Telegram `/rooms` explains the group route: add the bot to a Telegram group and tag it. Existing per-member private-action consent remains separate. Web rooms do not bridge Telegram groups, and personal shared actions are not enabled in web rooms. This is public conversation parity, not full group-action parity. In-app notifications still require opening the web app, not OS push.

## Private optional updates on both surfaces
Web Menu > Check-ins / digests reviews the private in-app destination, check-ins, morning/evening digests and quiet hours. Saving explicitly switches that account's optional updates to in-app Notifications. Telegram `/proactive on|off`, `/digest morning|evening|both|off`, `/quiet_hours START END` explicitly selects private Telegram delivery. No duplicate cross-channel delivery, inferred/group destinations, OS push or guarantee of exact free-host timing. Defaults stay off; each account must opt in. Standalone Google/email accounts get their own in-app inbox.

## Exact saved-fact removal
Web Menu > Forget saved fact reviews one exact key/value and removes it only after account/session-bound one-use confirmation. Changed values require a new review. Telegram `/forget KEY` continues to remove a named saved fact. This does not erase chat history, notes, provider copies or all account data. Full web account-data deletion remains unshipped; no all-data promise is made while computer file/identity cleanup is unresolved.

## Controlled free-form review on both surfaces
Web Connections > Review free HTML form and Telegram `/public_form ADAPTER | JSON` now use the same deployment-reviewed adapter checks. Each exact preview names the destination, terms and disclosed fields; web confirmation is additionally session-bound. Demo requires TEST ONLY identity and is not a real booking. Dynamic Calendly/Google appointment booking remains disabled and unaccepted. No paid form or unknown provider is silently substituted. Computer worker must be awake; no automatic retries after uncertainty.

## Public navigation bug-fix release
Public product navigation follows observed catalog links instead of guessing slugs. Duplicate cards with the same href are one distinct navigation target. Browser results include visible links and HTTP status;404 is not successful page verification. Follow-ups such as "open the details page and show me" are accepted and can reuse a recent per-account public catalog. Public cart/checkout GET views are allowed without cart changes, form submission, login, payment or order placement. Up to5 exact visible-link steps per request, no shell/unrestricted desktop expansion. Worker navigation protocol3 prevents old workers from silently serving the new flow; current verified heartbeat resolves stale starting state. Quotas and cold-start timing still apply.

## Agent workspace milestone
Web has a refreshed responsive workspace and live request/tool activity summaries. Activity contains execution labels only, never hidden reasoning, arguments or provider results. Account/request scoping remains. This release does not claim token streaming, voice/image generation or all consumer upgrades are complete.

## Theme/history and games milestone
Web has a persistent dark/light toggle and owner-scoped recent-request sidebar with access to recorded history. This is not independent chat-session memory. Telegram and web have `/play quiz`, `/play guess`, `/answer N`, `/guess N`, `/game_stop`; games are requester/chat-bound and expire after30minutes. Group games require a tagged bot command and do not import personal memory. Voice/image generation, true token streaming, personality preferences, natural follow-up expansion and MCP remain unshipped in this milestone.

Web: sidebar now shifts/resizes chat and composer instead of covering them. Optional Read aloud uses the browser/device speech service only; voice availability/privacy depends on device/browser. No server TTS or paid image route is enabled.

Named web conversation threads: separate owner-bound turn history, New chat/shared-history controls. Saved facts/tasks are still shared; named chat input is not added to the shared conversation summary. Telegram mirroring still applies.100-turn viewing limit. Legacy history preserved. Mail-watch scheduler literal-percent query fixed and exercised against local PostgreSQL.

Real LangChain model chunk streaming prepared for web agent generation, provisional visible text only (no thought/tool-argument blocks), encrypted owner/request-bound draft, replaced by checked final response. Browser reads snapshots every2seconds, not SSE. Direct-provider/fallback paths may still return a final blob. ElevenLabs adapter is opt-in/private only, Free-plan verified per call, shared10000credit budget,1200character requests, attribution, no automatic retry or paid fallback. API key/usable voice ID still required; no live audio acceptance yet.

Explicit companion preferences: /persona warm|concise|playful|coach; /nickname name|off. Private-only settings, no invented relationship/permission. /followup 24h topic schedules a requested private conversational check-in, deferred through quiet hours; no inferred milestone monitoring. Plugins /plugins and /mcp list capabilities. MCP default is zero remote servers: admin-configured public-read-only endpoints/tools/simple fields only, no user-added URLs/private history/resources/prompts/sampling/stdio. Supports legacy2025-03-26 Streamable HTTP only; no current-version universal claim. No provider connected for live MCP yet.

Upload composer correction: file selection stages a removable filename/image-preview chip, keeps typed context, and makes no upload request until explicit Send. Caption and file travel together; sent message shows file visually rather than raw upload receipt prose. Named-thread upload summaries preserve conversation binding. Inspected390/1280pixels and browser interaction assertions verify no auto-send, unchanged caption, and exactly one send request.

Composer Mic uses browser Web Speech recognition where available, with explicit notice that browser audio may go to its recognition service. Recognized text remains editable in the composer and does not send until Send. Stop/review flow, permission denial and unsupported-browser fallback are implemented. No paid STT/Gemini audio route enabled. Browser/mobile compatibility varies; mock event-flow acceptance and390/1280pixels inspected, actual microphone/service acceptance remains user/device-dependent.

Automatic short general reply audio: /voice on discloses third-party reply upload, shared cap, attribution and exclusions; /voice confirm enables it after review. Existing explicit-/speak opt-ins are not upgraded silently. Only private general model replies with no tool/error/confirmation metadata and at most1200characters qualify. Tool/account responses remain text-only. Text is sent before audio; speech failure keeps text and never retries/uses paid fallback. Web audio controls already render audio artifacts; actual Telegram voice acceptance still pending user/device confirmation.

New chat headers are auto-extracted from the first successful request (up to9topic words,70characters), replacing only generic names; explicit topic-shift requests can retitle that same thread. No extra provider call or account-data upload. This is an extractive header, not a model-written semantic summary. Never creates a thread automatically.
When configured, header generation uses the existing fixed openrouter/free zero-price/privacy-guarded router (40output-token limit, no tools), with extractive fallback. Google/account-keyword requests never go to the title model. No generic paid model route. Still only first exchange/explicit focus shift, not each turn.

October10 correction: automatic voice replies removed entirely, existing live reply-consent states disabled. /voice on now enables explicit speech only; /voice confirm cannot enable automatic outputs. /speak text or "say/read/speak TEXT as a voice note" are explicit requests. General replies stay text-only, regardless of previous opt-in. Ambiguous "say it" is not inferred from private history.

Explicit "write/draft/compose/prepare a mail/email" is locked into Google draft/clarification workflow even when intent parser returns none. It cannot silently produce an unreviewed ordinary-chat email in place of a Send/Cancel draft. Missing content still needs clarification; CC/BCC roles aren't inferred. No send without exact reviewed draft confirmation.

Web assistant output auto-links valid http/https URLs with new-tab noopener/noreferrer anchors, preserves text through text nodes (no innerHTML), excludes credentials and unsafe schemes, strips trailing sentence punctuation.390/1280mock-history pixels and unsafe HTML/javascript text acceptance inspected.

Work panel: rotating decorative status words, animated event rows, explicit done green checks and blocked warnings, collapsed settled panel on final outcome, reduced motion disables animation/word cycling. Backend tools emit completion only after ok+verified; preparation emits done after model return. No hidden reasoning/tool arguments.390/1280local pixels and transition fixture inspected; deployed acceptance pending.

Web composer explicit mail requests now prepare a session-bound web email review, with full From/To/CC/BCC/subject/body and Send exactly this/Cancel controls. Uses existing one-use exact-hash confirmation route; no Telegram callbacks or generic-chat send. Old web assets cannot fall through via "mail" anymore.407tests; local390/1280review pixels inspected. Live review+Cancel acceptance pending.

Workspace creation beta: per-user own Workspace connection, explicit create doc TITLE | text; create sheet TITLE | JSON rows; create slides TITLE | JSON title/body array. Exact Create/Cancel review before native new-file writes; no shares, no automatic retry, partial-file failure reported. Web composer supports same exact format and Menu > Connections > Create Doc/Sheet/Slides. Docs plain text, Sheets20x10RAW, Slides1-10native title/body layouts. Content readback only, visual inspection not claimed. No real Workspace create acceptance yet; currently0Workspace connections. Gmail connection alone does not grant Docs/Sheets access.

Supported OAuth consent upfront: Gmail read/send+Calendar events on Google connection; Docs+Sheets+Slides on separate Workspace connection. No full Gmail/delete, Drive-wide/admin or speculative future scopes. Identity-only login remains identity-only. Existing users may need reconnect; cannot guarantee never reconnect. Current console testing/verification state unverified this run. Google least-privilege policy precludes all possible scopes without actual features. No paid assessment initiated.

Quoted reply beta: Telegram native reply target text/caption and ID are injected as untrusted context, never approval; missing media/secret quote withheld honestly. Web Reply affordance stages/removes quote; server validates exact text in same owner+thread history before injection. No quote-driven actions. Future email send receipts include channel/recipient/subject audit (no body) for traceability.
