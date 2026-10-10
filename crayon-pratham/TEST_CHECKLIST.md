# Crayon test checklist

## Current web release - October 9, 2026, 20:10 IST

See [WEB_RELEASE_CHECKLIST.md](WEB_RELEASE_CHECKLIST.md) for the current done/pending/locked matrix and acceptance evidence. 317 local tests passed. Web login, shared ordinary history, uploads, waiting UI and safe reconnect/result recovery are deployed. Full feature parity and owner-device post-fix acceptance are pending. Older checkpoints below are historical, not current test counts or current web availability. WhatsApp is parked by owner request. No paid setup was made.


Covers the deployed bot (`cr_*.py`, Neon Postgres, Gemini, Tavily). Run these against a staging bot or your own chat.

Legend: **Verified live** = checked on the deployed bot through `/selftest` with synthetic users that were cleaned up afterwards (October 8, 2026). **Not live-tested** = reviewed in code only.

## Automated checks

- [ ] `GET /health` returns `{"ok": true, ...}` with `db: true`
- [ ] `POST /selftest` without a token returns `403`
- [ ] `POST /selftest` with `Authorization: Bearer <CRAYON_ADMIN_TOKEN>` runs a message through the real pipeline and returns results

```bash
curl https://crayon-v1.onrender.com/health
curl -X POST https://crayon-v1.onrender.com/selftest \
  -H "Authorization: Bearer $CRAYON_ADMIN_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"texts": ["hello"], "cleanup": true}'
```

`/selftest` uses a synthetic negative user id and captures replies, so nothing is sent to Telegram. Optional body fields: `reset`, `tick` (run one scheduler tick), `probe` (`reminders` or `facts`), `cleanup`.

## Feature checks

| Area | Check | Status |
| :--- | :--- | :--- |
| Memory | Tell Crayon a fact, start a new chat, ask for it back. `/memory` lists it. | Verified live |
| Memory | `/forget <key>` removes it and confirms | Verified live |
| Search | Ask for something current; the reply cites a source URL | Verified live (Tavily, with fallbacks) |
| Code | Ask for exact math; the answer comes from the sandbox | Verified live |
| Reminders | "Remind me in 2 minutes to ..." is delivered by the scheduler | Verified live |
| Scheduled jobs | "In 1 minute, search the web for ..." runs the search itself and sends the result | Verified live |
| Tracked tasks | A multi-step plan becomes a task with steps; marking a step done persists; "where are we on X?" recalls progress | Verified live |
| Task check-in | One nudge per quiet task per day, between 9am and 9pm local time | Not live-tested (needs a task untouched for 20h or more) |
| Confirmation | Forget a fact, reply NO: nothing changes. Forget again, reply YES: it is removed. | Verified live |
| Confirmation | A pending confirmation expires after 10 minutes | Not live-tested |
| Secrets | Paste something that looks like an API key or password; the message is deleted and not saved (a database search found zero stored rows) | Verified live |
| Honesty guard | The bot never says "saved" or "set" without a verified tool result in that turn | Not separately live-tested |
| Daily cap | After `CRAYON_DAILY_CAP` messages in a day the bot says the free-tier limit was reached | Not live-tested |
| Delete all | `/delete_my_data` asks for `confirm`; with it, everything is wiped and the bot reports the check | Not live-tested |

## Manual acceptance flow

1. Open [`@crayon_v1_bot`](https://t.me/crayon_v1_bot) and send `/start`, `/help`, `/memory`.
2. Tell it a preference, then ask about it in a fresh message.
3. Ask a question that needs the web and check that a source is given.
4. Set a reminder a couple of minutes out and wait for it.
5. Give it a multi-step plan, mark one step done, then ask where you stand.
6. Ask it to forget something; answer NO, then try again and answer YES.
7. Use two Telegram accounts and confirm memory never crosses users.

## Prototype tests

`pytest -q` runs the offline tests in `tests/`. They cover the original prototype code in `core/` and `tools/` with mocked model responses. They say nothing about the deployed `cr_*.py` bot.

## Phase 2

M7: `/goal <concrete research/calculation goal>` saves a 2-4 step plan and executes with up to 12 tools, repeat-call limits, and a time budget. Local runtime tests pass. Verified live: /goal calculated 17 x 23 with sandbox output, saved a note, updated both steps, and cleaned up the synthetic user. Admin form `/admin-test` uses captured output and requires the existing admin token. Selftest rejects real user IDs; scheduler excludes negative test users.

M8: photo/PDF/text/audio ingestion, 4 MB limit, untrusted-content analysis, no raw upload retention. Live image fixture verified; Telegram downloads and PDF/audio remain untested.
M9: /memory_review returns exact duplicate groups and evidence-backed preference suggestions, changes no facts. Verified live duplicate detection with two stored facts; no deletion.

M10 draft_message: review only, no external sender. M11 /proactive on|off: at most one stale-task or upcoming-reminder check-in daily. M12 /digest morning|evening|both|off and /digest_now: deterministic summaries. Defaults off, quiet hours 21-09 local; free-host delivery best-effort. Verified live with a synthetic clock: morning digest and upcoming-reminder check-in delivered once, repeat tick produced no messages. Draft fixture verified sent=false after fixing an invented excuse.

M14: content-based emoji selection and captured reaction calls. Command/YES/NO flows skipped. Telegram acceptance in a real chat not yet tested.
M8 update: synthetic PDF sentence read exactly. Real Telegram download path and actual spoken voice note not yet tested.
Runtime tests cover goal execution, invalid-tool handling, media size/type limits, duplicate detection, draft non-send, quiet hours, daily limits and reaction selection.

## Media/style expansion

- Local tests: 20 MB rejection boundary, unknown format receipt, safe ZIP listing, DOCX XML extraction, entity rejection, secret blocking, text truncation disclosure, video/caption routing, and final plain-text cleanup.
- Live verified in owner Telegram chat: new Yooooo fire reaction; photo red square + CRAYON TEST 42; immediate receipt and both timed progress lines; clean plain text; DOCX OCEAN 73; 5,880,039-byte text file LARGE 81 with truncation notice; later photo follow-up answered CRAYON TEST 42.
- Still pending live: video/large-binary Files API upload and confirmed deletion, exact 20 MB boundary, more formats. A real 15-second user MP4 (3,666,841 bytes) was read successfully through Files API; its event-ad text and summary were delivered at 22:47 IST. Independent provider deletion check remains pending.

M13 Google beta, October 8:
- 37 local tests pass, including user-bound encryption, replay, wrong-user draft access, tamper, timeout-no-retry and private-chat gate.
- Cloud client created; API enablement, scopes, Render secrets and live OAuth/read/disconnect verification still pending.
- Real email send is NOT tested; requires owner review of final recipient/content before any live send.
- Public verification and security assessment are NOT complete. No paid assessment authorized.

M15 prepared, not deployed: 41 local tests including exact group mention, ignored untagged groups, no private state and disabled group tools. Slow-text progress timer and revised prompt need owner-chat checks. Group tagging needs a real group test. Keep BotFather privacy mode ON; exact @crayon_v1_bot mentions are sufficient. No existing group was selected or messaged.
M13 2.16.0 live: health ok/db true. Privacy page visually inspected. Cloud credentials in Render; Gmail API enabled. Live captured smoke test passes /help and synthetic private-Google rejection. OAuth and real reads/send still untested; public verification not submitted.

October 8 23:15 IST live follow-up:
- 2.17.1 health ok=true/db=true.
- Personal OAuth completed; real /google_status, /gmail search (five IDs/headers), /gmail_read newsletter body and /calendar next-seven-days passed in owner's DM.
- Real one-line /email_draft self-addressed review-only fixture displayed exact content/hash; /email_cancel answered no send. Real send not tested; owner has a separate draft that was not touched by these tests.
- Slow text response delivered progress before clean final answer; revised prompt inspected on one HTTP/HTTPS sample, not a quality benchmark.
- BotFather says privacy ENABLED, with username mentions delivered. Left unchanged. Real group delivery not yet tested; no group chosen or messaged.
- Public Google verification not submitted; testing status remains. No payment/assessment initiated.

2.17.2: live health good, exact-content draft buttons visually inspected, Cancel callback passed in owner DM without send. Search Console URL-prefix ownership verification succeeded by homepage HTML tag. This is not OAuth app approval. Verification submission remains pending its end-to-end sanitized demo. No production publish, paid assessment or real test send initiated. His separately entered draft was left untouched.

## 2.18.0 conversational UX (October 8)
- 50 unique local tests pass, including absent/guessed recipient, forwarded input, no model inbox path, review-bound simple send and hidden draft IDs.
- BotFather privacy DISABLED confirmed. Real owner test group tagged greeting and tagged question answered. Untagged reply ignored. Membership welcome not live exercised.
- Friendly guide/reply keyboard, direct Google intent reads, encrypted pending recipient clarification, reviewed Send/Cancel and simple text confirmation. Single recipient, no CC/BCC/attachments. Previous draft invalidated on new review. Google results remain outside model and memory.
- Natural mail/calendar/draft, delete UI and reply keyboard require live readback; do not call all phrases proven. Existing commands remain power options.
- Real 2.18 chat proofs: natural `help` guide, `Email [owner address] saying ...` exact review with hidden IDs, Cancel button returned no-send, natural calendar returned events, natural mail search returned Google's no-matches result, privacy options displayed review/delete buttons.
- No new live send for `send it`; local review-pointer/UID/expiry gates tested. No destructive delete or disconnect performed. Welcome needs a future add event. Reply-keyboard layout still needs pixel inspection with keyboard opened.
- Privacy second-confirmation + Keep my data passed live, with unchanged-data acknowledgement. No destructive test.
- Reply keyboard opened and visually inspected: all 6 choices legible in 2 columns. Natural calendar recheck uses readable day/date/time in 2.18.1.

## Phase3 (October8-9, latest state supersedes earlier pending entries)
- Public pasted example.com read verified in real Telegram. First deep-research comparison returned links and403 disclosure but uneven subjects; improved per-subject bounded search and citation instruction, second live proof pending.
- Frankfurter10USD ->966.30INR dated2026-10-08/source verified. Fixed singular unit aliases and real1mile ->1.609744km passed.
-2.20.1 natural 'Please connect my Google account' returned a real tappable connection link without reply-keyboard dependence.
- Four Google tester addresses persist in Audience; External/Testing unchanged. No production publishing.
- Current78 local tests include bounded CSV, formula escaping, chart validity, mocked sendDocument confirmation, text aliases, short-connect state-only fixed destination, used/expired rejection.
-2.21.0 CSV/chart and short link are built/deployed; real transport/redirect proof is in progress. No claim of all live checks yet.
-6workers is a code ceiling, not measured comfortable user capacity. Project-wide Gemini free quotas verified15/250k/500(primary),15/250k/500(fallback1),5/250k/20(fallback2). API requests are not chat replies.

2.21.1 incident follow-up: owner forwarded own Google link; live DB/status confirmed Sibi Google under owner1898030979. Owner approved disconnect; bot confirmed Google revocation, credentials/drafts removal, independent DB empty +status none. Later7555366869 independently connected Sibi Google; untouched. No mailbox/calendar content retrieved for diagnosis. Historical 'Plz connect to my Google' missed deterministic alias and got a false general-model denial; added plz/pls/to variants and tests.81 tests pass. Short link live issuance and Google fixed-client redirect passed; invalid state400 passed. No new OAuth consent performed by agent. Files/research post-fix live proof remains pending due to incident priority.

## Phase3 computer/browser and closure (Oct9,00:47IST)
-2.23.1 live health;97 local tests pass, including progress race, owner-only computer/path/arithmetic checks and browser denylist cases.
- M19: CSV50B/chart15.3KB received in owner Telegram. Delivered chart inspected at full size: Maths3/Finance5/Strategy2, clean labels, units and supplied-data disclaimer.
- M17: initial retest still searched unrelated Obsidian Energy/Notion-vs-Monday. Fixed compared-name extraction preserving purpose context and official-source preference. Subsequent real retest uses Notion education/help and Obsidian/help. Model claim/citation synthesis remains fallible.
- M20: GitHub Pro student, fresh Codespaces budget$0/Stop usageYes, consumed/billable$0 checked before creation. Created2-core Southeast Asia Linux Codespace, no public ports. Outbound worker dedicated bridge token, owner-only fixed operations,25-minute session cap, no auto-restart.
- Real computer status +20*(3+2)/4 returned25. Real Chromium Python tutorial task delivered159KB crayon-browser.png and URL log to owner Telegram at00:47. Full delivered-image pixels inspected: readable Python tutorial and sidebar.
- Browser initial bridge failures produced fake model '(screenshot attached)' text with no attachment. Deterministic screenshot routing now requires verified result AND PNG bytes; fail-closed errors instead.
- Remaining: real group welcome-on-add, real computer file create/read (initial operation error fail-closed; schema enum fixed, retest pending), README rendered final phase table inspection. Public Google verification still pending, no paid assessment initiated.

Tester beta: owner+approved-tester UID gate;5 execution jobs per tester/day,20 owner/day,30 total/day, atomically reserved before jobs including failures. Automatic wake is enabled after real October 9 cold-start/worker/calculation/idle-stop proof. Additional testers are not enabled until their Telegram IDs are verified.203 local tests in the current suite; tester behavior code-tested, no impersonation or live test from another person's chat.

## Public-web and official chart chain (Oct9,01:20IST)
-2.25.0 public-web:98 local tests, deployed; fresh$0 Codespaces consumed/billable and$0 stop budget checked before restarting existing machine.
- Owner Telegram Instagram screenshot281KB, full pixels checked: blank login fields; no sign-in. YouTube screenshot18.7KB full pixels checked: signed-out search/sign-in/try-searching homepage. No search/playback claim.
-2.26.0 World Bank API three-country2024 nominal GDP per capita -> exact-record validation -> source screenshot45.3KB, CSV466B, chart21.9KB -> findings/source/action log. Delivered real Telegram at01:19; full chart pixels checked. Source updated2026-10-08.
-At the earlier102-test milestone, tests included exact source values, missing/mismatched/duplicate records, and narrowly scoped recipe browser intent. Synthetic tests are not live proof.

## Google autonomy and menu (Oct9,01:34IST)
-2.27.0 deployed,112 local tests. Owner mail watch opt-in remains off; no scheduled Gmail check live proof yet.
- Private primary calendar preview + Cancel live Telegram proof passed; no Create clicked, no event written, owner write-scope reconnect remains needed.
-17-command menu rendered/read back in Telegram, actual popup pixels inspected.
- Owner text-file create/read followed by independent read-only request returned exact test contents. Test prompts agent-authored under general testing request, not user approvals.
- Computer restarted for owner active testing, heartbeat awake verified, owner eikcai.com screenshot493.4KB delivered. Auto-wake token creation blocked by GitHub error, no token yet.

## October 9 current-state checkpoint (supersedes older historical milestones)

203 local tests pass. Real Telegram queue arithmetic, Show results/TXT export, dashboard recorded completion/CSV/chart, silent-audio no-speech, blue-video and PDF regression passed. Gemini stays primary; free-only OpenRouter forced-failure proof passed, automatic fallback enabled and deployed 14:41 IST. Owner hourly mail watch delivered a real two-item Telegram notification at 15:00 IST; checked advanced from 13:59:59 to 15:00:11, seen_count 0 to 2, not paused.

Calendar reads, preview/create/cancel/reconnect are owner-only. Optional guests and popup reminders have exact preview and payload/readback regressions. Live preview/Cancel passed with no event; Google write-scope reconnect and actual reviewed Create remain unproven. Guest notifications only after Create; no Meet.

Public form foundation implemented for a controlled demo. Local fresh Chromium verifies inspect without POST, changed-page rejection without POST, exact reviewed submission with one POST and receipt. Live worker demo preview passed15:04; Submit awaits exact owner approval. Dynamic Google appointment/Calendly slot adapter remains open. Never claim provider booking from generic fixture evidence.

Groups support bounded public lookup for anyone who tags the bot. News is dated Google News index headlines plus publishers, not independently verified article summaries. Private account reads, memory, jobs and writes remain excluded. Real owner group news acceptance passed October 9 at 14:32, then owner requested text-only UX without links.

No paid activity initiated. Google public verification/possible restricted-scope assessment and outside-tester acceptance remain external gates. WhatsApp on this Telegram instance was stopped; optional shared code is maintained in the same repository.

Latest 15:02 checkpoint: worker Playwright/Chromium and Debian browser libraries installed; direct controlled form inspection verified with zero POST. The real Telegram preview at 14:57 failed because a 14:56 idle-stop left a fresh cached heartbeat. Lifecycle fix clears readiness on accepted start/stop and serializes readiness/activity/enqueue against idle-stop. Full suite: 203 passed. Real Telegram preview4402 and screenshot4403 passed at15:04 after cold-start. No Submit/POST; exact controlled demo awaiting owner review. Awake-but-exited worker needs a host restart or explicit supervisor start; API start on an already-awake host does not rerun postStart. Public-news topic/date/relevance/publisher cleanup is deployed, but post-fix group acceptance is still pending.

15:14 controlled-demo closeout: original owner approval inspected; identical TEST ONLY fields reviewed in fresh preview4406, clicked Submit once once, receipt4408 and screenshot4409 show Test form received at the configured destination. No real booking or calendar action. Calendar reconnect link prepared for owner, actual Create still requires a reviewed preview and approval.

## WhatsApp and group/email hardening (October9, 15:50)
-223tests pass. WhatsApp v2.34.0 live1010c52: signed verify+POST, exact phone/allowlist, durable inbox dedupe, monotonic statuses by accepted wamid, no template/outside24h sends, uncertain processing no replay. Meta resources/two-way still pending cooldown.
-Group/email v2.35.0 maine23699b: per-member group audience opt-in, requester/chat/control binding, no cross-chat private compose/history, no group connection links/proactive reroute. Complete subject/body generation from intent, no fabricated facts. Group live member test pending.
-No real outgoing email or calendar event performed by automated tests.
