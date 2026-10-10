<div align="center">

# 🖍️ Crayon v1

<img src="https://readme-typing-svg.demolab.com?font=Fira+Code&weight=600&size=24&duration=2800&pause=900&color=FFC93C&center=true&vCenter=true&width=720&lines=A+Telegram+assistant+that+remembers.;Searches+the+web.+Runs+code.+Keeps+reminders.;Tracks+multi-day+tasks.+Admits+what+it+didn't+do." alt="Animated Crayon introduction" />

**A personal assistant on Telegram and the web with persistent memory, live web search, sandboxed code, reminders, tracked tasks and honest safety rails. Runs entirely on free tiers.**

![MIT license](https://img.shields.io/badge/license-MIT-FFC93C?style=for-the-badge)
![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB?style=for-the-badge&logo=python&logoColor=white)
![Telegram](https://img.shields.io/badge/Telegram-long_polling-26A5E4?style=for-the-badge&logo=telegram&logoColor=white)
![Gemini](https://img.shields.io/badge/Gemini-Flash_Lite-8E75B2?style=for-the-badge&logo=googlegemini&logoColor=white)
![Postgres](https://img.shields.io/badge/Neon-Postgres-00E599?style=for-the-badge&logo=postgresql&logoColor=white)
![Render](https://img.shields.io/badge/Render-free_tier-46E3B7?style=for-the-badge&logo=render&logoColor=black)
![Cost](https://img.shields.io/badge/cost-%240_free_tiers-22A06B?style=for-the-badge)

![Codespaces](https://img.shields.io/badge/Codespaces-per_user_beta-FFC93C?style=for-the-badge&logo=github)
![Browser](https://img.shields.io/badge/Chromium-public_web_beta-2B2D31?style=for-the-badge&logo=googlechrome)
![Tests](https://img.shields.io/badge/local_tests-377_passed-22A06B?style=for-the-badge)
![Pages](https://img.shields.io/badge/GitHub_Pages-web_live-FF6B6B?style=for-the-badge&logo=githubpages&logoColor=white)
![Login](https://img.shields.io/badge/Telegram_login-7_days-26A5E4?style=for-the-badge)
![Parity](https://img.shields.io/badge/full_parity-in_progress-FFC93C?style=for-the-badge)

[**Open web app**](https://itsppm76.github.io/crayon/) · [Release checklist](WEB_RELEASE_CHECKLIST.md) · [**Try the bot**](https://t.me/crayon_v1_bot) · [Features](#-what-crayon-can-do) · [Architecture](#-architecture) · [Safety](#-safety-rails) · [Endpoints](#-endpoints) · [Setup](#-setup) · [Limits](#-honest-limits)

</div>

---

## Current release: October 10, 2026

[Final Day 0-to-current feature checklist](LIVE_FEATURE_CHECKLIST.md) · [Web release checklist](WEB_RELEASE_CHECKLIST.md) · [Changelog](CHANGELOG.md)

Per-user access release opens bounded computer/browser and private text files to every authenticated user. Calendar and mail-watch use each user's own connected Google account, with exact review and opt-in. No one gets the owner's data. Google Testing still limits data-access consent. Telegram login, independent Google-first login and verified email/password login are configured. An already-linked Google identity opens its existing account; otherwise accounts keep separate chat and memory. Matching email addresses never trigger linking. Login does not connect Gmail, Calendar or Workspace.

**Account consolidation is OFF by owner choice. No account merge happened.** The stopped scoped-test/two-Google changes were never deployed. Existing Telegram history and connections stay intact.

Web chat, history, uploads, generated files, seven-day sessions, visible-tab sync, waiting UI and original-request result recovery are deployed. Telegram-backed accounts share their ordinary chat/memory between Telegram and web. Standalone accounts have no Telegram mirror destination. Private Google results remain outside the chat model and shared memory.

377 local tests pass. Live health confirms database/polling; live web status confirms all three login configurations and uploads. Real standalone Google/email and password-reset acceptance are not claimed. Owner-device post-fix reconnect/upload delivery remains pending.

Standalone reminders/scheduled jobs/work completion now appear in private in-app Notifications, not system push while closed. Web computer/browser and /work controls are enabled. Web shared public rooms, optional check-ins/digest controls and exact saved-fact removal reviews are deployed. Private group-action parity and full web data deletion remain pending. Gmail is a named-tester beta, calendar is per-user, and actual calendar Create remains unproven. WhatsApp is test-only; production setup stopped at payment-method/business requirements under the no-card rule. No paid setup or payment was made.

The checklist separates deployed features, real acceptance evidence and disabled/setup-dependent paths. Older milestone sections below are historical and do not override it.

---

## ✅ Earlier October 9 Telegram milestones (historical)

Owner/tester gate notes below describe previous milestones. Current access is in the final checklist: every Telegram user gets bounded computer/browser/private files; Google features use only that user's connection, subject to Google Testing.

Earlier milestone: 203 tests passed then. Current local suite: 377. Deployed bot: [@crayon_v1_bot](https://t.me/crayon_v1_bot). Feature availability is separate from proof: code-tested features are not marked as live user tests.

### Overnight task power (2.33.1)

- `/tasks add Title | step1 | step2`: deterministic task creation. `/tasks export`: task CSV and progress-chart PNG, with recorded-results caveat.
- `/tasks`: private task dashboard with progress, blocked steps, next step, reminders and internal-work queue. `/tasks show ID` shows recorded results; `/tasks done ID STEP | note` records your own completion note, not an independently verified result. Doing/blocked/todo use the same syntax and can reopen a finished task.
- `Background research: topic` is a plain-language shortcut for the same explicit brief queue; `my work queue` shows status.
- `/work brief topic`: queues a bounded assignment starter from fetched public sources: answer, sourced points, possible outline and gaps. AI draft, not submission-ready; exact fetched URLs are checked, claim support remains fallible.
- `/work plan Title | research topic | calculate 2+2` : 1-3 explicit internal steps. Supported: public research/source receipts, public page text, bounded Decimal arithmetic (28-digit precision) and briefs. No Google, shell, website actions, email/calendar writes or messages to other people.
- `/work list`, `show ID`, `pause ID`, `resume ID`, `cancel ID`, `export ID`: persistent progress and TXT report/CSV source ledger. 3 active jobs and 3 new jobs/person/24h, 10 total/24h. Research runs on its own bounded worker thread so it does not hold up the reminder loop. State-bound Show/Pause/Resume/Cancel/Export buttons are built; callback controls code-tested, real Show results and Export TXT buttons passed October 9. Uncertain completion-message delivery is not automatically retried; results remain available in the queue. Interrupted steps block without retry; quiet hours delay completion notifications. Default quiet hours 21:00-09:00.
- Scheduled model jobs are read-only at tool enforcement. New internal queue has live deployed captured tests for two-step queue, brief, work TXT/CSV export, pause/resume/cancel, cross-user isolation, task creation/dashboard/chart-export and quiet-hour deferral (real database/source/model paths, no Telegram send). Real Telegram two-step arithmetic queue, single completion, Show results and TXT export passed October 9. General brief claim support remains fallible.
- Inbox attention reports now have short sections and decoded HTML entities; owner's real Telegram rendering checked.

### Who gets what

| Access tier | Available features | Limits |
| :--- | :--- | :--- |
| Everyone using the bot | Chat, personal memory/review/forget/wipe, notes, reminders, tracked tasks, goal plans, web search/page reading, bounded research, sandboxed maths, currency/unit conversion, CSV/charts, review-only message drafts, opt-in digests/nudges and quiet hours | Each person's private data stays separate. Free-host timing and model output are best-effort. |
| Named Google test users who connect their own account | Gmail reads and encrypted reviewed email Send/Cancel on their own connection; calendar is per-user | Google OAuth is still in testing mode, not open to arbitrary accounts. Each user needs their own personal connection link. Calendar Create needs optional write-scope reconnect and an approved live test. |
| Every Telegram user (per-user limits) | Computer status, arithmetic, public HTTPS browser screenshots; fixed World Bank research-to-chart demo | 5 execution jobs/tester/day, 20 owner/day, 30 total/day, failures included. Shared free machine must be awake. Tester access code-tested; another person's live test still pending. |
| Each user, private folder | Text-file create/read on the computer; lifecycle/setup controls; opt-in hourly email-watch beta | Mail watch was enabled October 9 at 13:59 IST. Real two-item Telegram notification passed October 9 at 15:00 IST. Owner files are not shared with testers. |

### Live checks and UX

- Public screenshots of the Python tutorial, Instagram login wall and signed-out YouTube were delivered and visually checked. This is public-site reading, not a general website operator.
- The fixed World Bank 2024 GDP-per-capita demo returns checked India/China/US records, source screenshot, CSV, chart, findings and work log. Not arbitrary research automation.
- Owner computer status, arithmetic and text-file create/read passed live tests.
- Google mail/calendar reads, owner-reviewed email send, and calendar preview/Cancel passed live checks. No calendar event write claimed.
- Requests such as "Check pending tasks and emails that need attention" use a private bounded inbox-metadata path, separate from the chat model. It lists Crayon tasks and checks up to4 messages from the past7days; not a complete inbox audit and does not turn scheduled checks on.
- Group exact-tag replies and untagged-ignore passed live tests; add-welcome still needs a real add test.
- 19-command Telegram menu, plain-text replies, reactions and progress messages are deployed.
- PNG screenshots/charts use Telegram `sendPhoto` within photo dimensions; CSVs stay documents. Existing artifact limit is 2MB. Photo-card delivery and rendered in-chat screenshot pixels were verified live.

### Still coming / not active

| Feature | Exact status |
| :--- | :--- |
| Automatic computer wake | Enabled after real cold start, worker-ready, calculation and idle-stop proof October 9. Free quota, stop-usage budget and idle policy remain. |
| Hourly email watch | Deployed owner-only opt-in beta, enabled October 9 at 13:59 IST. Bounded sender/subject/provider-snippet excerpts, quiet hours, no bodies or Gemini processing. Real two-item Telegram notification passed October 9 at 15:00 IST. |
| Calendar Create | Deployed, awaiting optional Google write-scope reconnect and exact owner-approved live Create. Owner primary calendar only; optional explicit guest invites and popup reminders after exact Create. No Meet. |
| Group welcome / other-user computer proof | Code-tested; real group-add and tester-chat checks pending. |
| Public form/booking interaction | Reviewed public HTML-form foundation and controlled demo implemented. Local fresh-Chromium exact-one-POST acceptance passed. Live worker demo and dynamic Google appointment/Calendly adapter acceptance remain open. No provider booking claimed. |

Safety remains: review before sends/writes, secret interception/redaction, per-user isolation, bounded jobs/tools/reads, untrusted-page handling, public-IP and sensitive-portal checks. Google mail/calendar results bypass Gemini. $0 budget guard unchanged; no payment initiated.

## 🟡 What Crayon can do

Crayon lives in a Telegram chat at [`@crayon_v1_bot`](https://t.me/crayon_v1_bot), hosted at <https://crayon-v1.onrender.com/>. The core capabilities below are deployed; release notes above separate live checks from inactive and code-tested features.

| # | Capability | What it means in practice |
| :-: | :--- | :--- |
| 1 | 🧠 **Persistent memory** | Facts, preferences and projects are stored in Neon Postgres and recalled across chats and restarts. See and edit them with `/memory` and `/forget`. |
| 2 | 🔎 **Web search** | Current information through the Tavily API, with DuckDuckGo, Mojeek and Wikipedia as fallbacks. Can also open a public page and read its text. |
| 3 | 🧪 **Sandboxed code execution** | Exact math and data checks run in a Python sandbox with no internet, files or access to your data. |
| 4 | ⏰ **Reminders and scheduled jobs** | One-off or recurring reminders, plus self-running jobs that fire a prompt on schedule. Delivered by an in-process polling scheduler. |
| 5 | 📋 **Multi-day tracked tasks** | Tasks with subtasks and per-step status (`todo`, `doing`, `done`, `blocked`). Progress is injected into the conversation, and a daily check-in nudge follows up on open tasks. |
| 6 | 🛡️ **Safety rails** | Confirmation before irreversible actions, automatic deletion of messages that look like secrets, and an honesty guard that blocks unbacked "done" claims. |
| 7 | 🩺 **`/health` and `/selftest`** | A public health check and a token-protected end-to-end self test. |

### Chat commands

| Command | Does |
| :--- | :--- |
| `/start` | Greets you and registers your profile |
| `/help` | Lists what Crayon understands |
| `/memory` | Shows everything Crayon remembers about you |
| `/forget <key>` | Removes one remembered fact, then checks it is gone |
| `/delete_my_data confirm` | Permanently deletes your memory, notes, reminders and chat history |

Everything else is natural language: "remind me tomorrow at 9 to call the bank", "track my passport renewal as a task", "what's the latest on ...".

### Tools the model can call

| Group | Tools |
| :--- | :--- |
| Memory and notes | `remember`, `forget`, `save_note`, `list_notes` |
| Time and reminders | `get_time`, `set_reminder`, `schedule_job`, `list_reminders`, `cancel_reminder` |
| Tracked tasks | `create_task`, `list_tasks`, `update_step`, `close_task` |
| Web and compute | `web_search`, `read_url`, `run_python` |

Each tool is tagged with a risk level (safe, write or dangerous). Dangerous ones, such as `forget`, require an explicit YES first.

---

## 🔵 Architecture

```mermaid
flowchart TD
    U[You on Telegram] -->|long polling| TG[cr_telegram]
    TG --> SEC{Looks like a secret?}
    SEC -->|yes| DEL[Delete message, never store it]
    SEC -->|no| AG[cr_agent: tool loop, max 6 calls]
    AG --> LLM[Gemini 2.5 Flash + Flash-Lite fallback]
    AG --> TOOLS[cr_tools]
    TOOLS --> MEM[(Neon Postgres)]
    TOOLS --> WEB[cr_web]
    WEB --> TAV[Tavily]
    WEB --> FB[DuckDuckGo / Mojeek / Wikipedia]
    WEB --> SB[Python sandbox]
    AG --> HG{Honesty guard}
    HG --> U
    SCH[cr_sched: polling scheduler] --> MEM
    SCH -->|due reminders, jobs, daily nudges| TG
    HOST[main.py: /health, /selftest] --> MEM
    classDef yellow fill:#FFC93C,color:#2B2D31,stroke:#2B2D31
    classDef blue fill:#DDF1FF,color:#2B2D31,stroke:#3776AB
    class AG,LLM yellow
    class TG,TOOLS,WEB,SCH,HOST blue
```

### Stack

| Layer | Choice | Cost |
| :--- | :--- | :--- |
| Interface | Telegram Bot API, long polling | Free |
| Hosting | Render web service (free tier) | Free |
| Database | Neon Postgres (free tier) | Free |
| Model | Gemini 2.5 Flash, with `gemini-2.5-flash-lite` as fallback | Free tier |
| Search | Tavily API, then DuckDuckGo, Mojeek, Wikipedia | Free tier |

### Modules

| File | Role |
| :--- | :--- |
| `main.py` | Entry point: HTTP server for `/health` and `/selftest`, database setup, Telegram polling |
| `cr_telegram.py` | Telegram transport, command handling, per-user daily message cap, secret interception |
| `cr_agent.py` | Agent loop, system prompt, confirmation flow, honesty guard |
| `cr_llm.py` | Gemini client with model fallback |
| `cr_tools.py` | Tool registry and implementations (memory, reminders, tasks, web, code) |
| `cr_memory.py` | Facts, preferences, history and data deletion |
| `cr_db.py` | Neon Postgres access and schema (users, facts, messages, notes, reminders, tasks, subtasks, pending actions, audit log) |
| `cr_web.py` | Search with fallbacks, page reader, code sandbox |
| `cr_sched.py` | Background scheduler for reminders, jobs and daily task check-ins |
| `cr_safety.py` | Secret detection and log redaction |
| `cr_selftest.py` | End-to-end self test behind `/selftest` |
| `cr_config.py` | All settings, read from environment variables |

> [!NOTE]
> The repo also contains the original prototype layout (`core/`, `adapters/`, `tools/`, `db/schema.sql`, `notebooks/`). The deployed bot runs the `cr_*.py` modules above.

---

## 🧭 Design choices

**Polling, not webhooks.** Both the Telegram connection and the reminder scheduler poll. This was deliberate: it needs no public webhook registration and survives the free host sleeping and waking. A keep-warm ping keeps the Render service awake so the scheduler keeps running.

**Memory is explicit and inspectable.** Crayon stores what you ask it to remember. `/memory` shows it all, `/forget` removes one item, and `/delete_my_data confirm` wipes everything and verifies the result.

**Progress injection for tasks.** Open tasks and their step status are fed back into the model's context, so a multi-day task survives across chats without you repeating yourself. A check-in nudge (at most about one per task per day, during waking hours in your timezone) reminds you of active tasks that have gone quiet.

**Free by default.** Every component runs on a free tier. A per-user daily message cap protects those limits.

---

## 🛡️ Safety rails

| Rail | Behaviour |
| :--- | :--- |
| **Confirmation for irreversible actions** | Dangerous tools do not run on the model's say-so. Crayon stores a pending action and asks you to reply **YES** or **NO**. It expires after 10 minutes. |
| **Secret auto-deletion** | If a message looks like a password or API key, Crayon deletes it from the chat, does not save it, logs the event and tells you to rotate the secret if it was real. Known server secrets are also redacted from logs. |
| **Honesty guard** | The model may not claim something is saved, set or deleted unless a tool result in that same turn was verified. If it claims "done" without backing, the reply is replaced with a plain statement that nothing ran. |
| **Verified deletes** | `/forget` and `/delete_my_data` re-check the database and say so if anything remains. |
| **Untrusted web content** | Search snippets and page text are treated as data, never instructions. |
| **Bounded tool loop** | At most 6 tool calls per message. |
| **Audit log** | Confirmations, declines and blocked secrets are written to an audit table. |

---

## 🩺 Endpoints

| Endpoint | Auth | Returns |
| :--- | :--- | :--- |
| `GET /health` | none | JSON with `ok`, `version`, `db` (database reachable) and `mode` |
| `POST /selftest` | `Authorization: Bearer <CRAYON_ADMIN_TOKEN>` | Sends test messages through the real pipeline (live Gemini, live database) as a synthetic user, with nothing sent to Telegram. Can also trigger a scheduler tick and probe reminders or facts, then clean up. Returns `403` without the token. |

```bash
curl https://crayon-v1.onrender.com/health
```

---

## 🟢 Setup

You need a Telegram bot token, a Gemini API key and a Postgres connection string. Search works without Tavily but is better with it.

```bash
git clone https://github.com/itsppm76/crayon.git
cd crayon
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Set these environment variables (a local `.env` works; never commit it):

| Variable | Required | Purpose |
| :--- | :-: | :--- |
| `TELEGRAM_BOT_TOKEN` | yes | Token from `@BotFather` |
| `GEMINI_API_KEY` | yes | Google AI Studio key |
| `DATABASE_URL` | yes | Neon Postgres connection string |
| `CRAYON_ADMIN_TOKEN` | for `/selftest` | Bearer token guarding `/selftest` |
| `TAVILY_API_KEY` | recommended | Tavily search; without it, fallbacks are used |
| `GEMINI_MODEL` | no | Default `gemini-2.5-flash` |
| `GEMINI_FALLBACK_MODELS` | no | Default `gemini-3.1-flash-lite,gemini-3-flash-preview` |
| `CRAYON_TZ` | no | Default timezone, default `Asia/Kolkata` |
| `CRAYON_MODE` | no | `polling` (default) |
| `CRAYON_PUBLIC_URL` | no | Public URL used for keep-warm |
| `CRAYON_DAILY_CAP` | no | Messages per user per day, default 80 |
| `CRAYON_HISTORY_TURNS` | no | Chat turns kept in context, default 14 |
| `CRAYON_VERIFY` | no | `1` (default) runs a one-pass self-check on factual answers; `0` turns it off |

Run it:

```bash
python main.py
```

**Deploy on Render:** create a free web service from this repo, set the variables above, use `python main.py` as the start command, and let Render supply `PORT`. Check `/health` once it is up.

---

## 🟠 Honest limits

- It is a single-user-scale project on free tiers. Free hosts sleep, free models rate-limit and Render's free instance can be slow on a cold start.
- Reminders are delivered by an in-process poller, so timing is close but not exact, and a long outage delays them.
- The code sandbox has no network or file access by design, so it cannot fetch data.
- Search quality depends on Tavily and its fallbacks; snippets can be wrong or stale.
- The honesty guard and secret detector are heuristics, not proofs. Rotate any secret that was pasted into a chat.
- Google testing-mode integration is live: per-user OAuth, direct Gmail/primary Calendar reads and exact-content review-first email drafts. Owner-confirmed real send was proven. Public verification remains pending.
- Test coverage in `tests/` is from the earlier prototype (see [`TEST_CHECKLIST.md`](TEST_CHECKLIST.md) for live checks) and mocks model responses. Live behaviour is checked through `/selftest` and the bot itself.

See [final live checklist](LIVE_FEATURE_CHECKLIST.md) for current status; older roadmap and milestone notes are historical.

---

## ⚪ License

MIT © 2026 Crayon contributors. Read [`LICENSE`](LICENSE).

<div align="center">

**Crayon Yellow `#FFC93C` · Charcoal `#2B2D31` · Slate `#6B7280`**

Small assistant. Honest status. Room to grow.

</div>

## 🟣 Agentic features, phases 2 and 3 (historical milestone details)

| Layer | What exists | Status and boundaries |
| :--- | :--- | :--- |
| 🎯 Goal mode | Saved plans, bounded tools, per-step result tracking | Deployed;12-call goal budget, no invented actions |
| 📚 Media | Photo, PDF, voice, audio, video, text/code, common Office extraction, archive listing |20MB Telegram cap; local/provider limits; raw uploads not retained |
| 🧠 Memory review | Duplicates and preference suggestions for review | No silent merges or deletes |
| ✍️ Message drafts | Recipient/fact-grounded review-only drafts | No third-party message auto-send |
| 🌅 Daily controls | Opt-in proactive nudges, morning/evening digests, quiet hours | Free-host timing is best-effort |
| 💛 Chat UX | Local reactions, plain text, natural-language controls, buttons, timed progress | Serialized progress prevents late fillers after response |
| 👥 Group chat | Exact mentions; requester-owned actions after member audience opt-in | Tagged reply and untagged-ignore live-proven; add-welcome code exists, live add test pending |
| 🔐 Google beta | Encrypted per-user OAuth, short personal links, Gmail and primary Calendar reads | Named testers only,7-day token expiry possible; no mailbox data sent to Gemini |
| 📧 Reviewed email | Ten-minute encrypted drafts, exact-content Send/Cancel, stale-button protection | Owner reviewed real send proven; multi-To/CC/BCC reviewed; no attachments or uncertain-send retry |
| 🌐 Public reader | Bounded HTTP page reads, redirects checked, explicit failures |1MB/12k-character caps; no login/JavaScript/paywall bypass |
| 🔎 Deep research | Bounded multi-source reads; comparison subject/context searches | Official Notion/Obsidian comparison retested; citations/model synthesis remain fallible |
| 🧮 Conversions | Dated Frankfurter currency and Decimal unit conversion | Real currency/unit tests passed; not a live FX quote, fees excluded |
| 📎 File output | CSV and non-negative bar chart PNG from supplied/checked data | Both received in real Telegram; delivered chart pixels checked |
| 💻 Computer beta | Shared2-core Codespace, outbound bridge, bounded arithmetic/text-file tasks | Live status/calculation passed; no shared-user shell, no paid budget or auto-restart |
| 📸 Browser beta | Fresh public Chromium, public-IP checks, sensitive-portal backstop, two-page limit, screenshot and URL log | Python tutorial, Instagram login wall and signed-out YouTube screenshots received in Telegram and visually inspected |

### Try it in plain language

```text
Remind me tomorrow at8 to call Mum.
Help me plan a project and track the steps.
Any new mail from Alex?
What's on my calendar?
Connect Google.
Go deep on a comparison of Notion and Obsidian for student notes.
Convert10 USD to INR.
Create a CSV and chart using Maths3, Finance5, Strategy2 hours.
On my computer browser, open https://docs.python.org/3/tutorial/ and send a screenshot.
Visit https://www.instagram.com and send a screenshot of the login wall.
Run world bank research chart demo.
```

Slash commands remain optional power controls: `/goal`, `/memory_review`, `/proactive`, `/digest`, `/quiet_hours`, `/connect_google`, `/google_status`, `/disconnect_google`, `/gmail`, `/gmail_read`, `/calendar` and review-only email commands.

### Computer and browser are a beta, not an unrestricted operator

The owner's GitHub student allowance includes180core-hours, about90actual hours at2cores, and20GB-month storage. A$0 Codespaces budget with stop-usage was verified before creation. Compute is used while awake; storage while the Codespace exists. The outbound worker session defaults to25minutes. A deployment-controlled session can be bounded up to6hours for an approved build; the bot cannot change it. Auto-wake is enabled after real cold-start and idle-stop acceptance. Manual start is still a recovery option. Stop when finished. Never raise the budget as a workaround.

Browser can visit public HTTPS sites, including Instagram and YouTube. Login/access walls are captured honestly. Known banking, webmail, patient and transaction portals are blocked by hostname/path rules; this backstop is not a perfect category classifier. No imported account cookies, no forms, logins, posting, purchases, downloads or unrestricted scripts. Only users within quotas can use browser/arithmetic. Owner files stay owner-only. Details: [COMPUTER_SETUP.md](COMPUTER_SETUP.md).

### Official-source research-to-chart demo

`Run world bank research chart demo` reads the official World Bank API for India, China and the United States,2024 GDP per capita in current US dollars. It validates country/year/indicator/value before generating a CSV and chart, then returns source screenshot, findings and an action log. Real Telegram chain passed. This is one fixed recipe, not arbitrary research automation. Missing, duplicate or mismatched records fail closed. Nominal GDP per person is not personal income, purchasing power or a living-standard ranking. Requires the computer to be awake.

### Google connection rules

Each user requests their own short link in their own private Crayon chat. Never forward an account-bound connection link. Choose your own named tester email and review Google's permissions. Invalid/used/expired states fail closed. `/disconnect_google` removes stored credentials and pending drafts and attempts Google revocation.

Privacy: https://crayon-v1.onrender.com/privacy. Terms: https://crayon-v1.onrender.com/terms. Public Google verification and any restricted-scope assessment remain pending. Nothing paid has been initiated.

Tester beta: owner+per-user UID gate;5 execution jobs per tester/day,20 owner/day,30 total/day, atomically reserved before jobs including failures. Automatic wake is enabled after October 9 lifecycle proof. Additional testers are not enabled until their Telegram IDs are verified.203 local tests, tester behavior code-tested, no impersonation or live test from another person's chat.

### Mail checks and private calendar previews (2.27.0)

Say `turn on email checks` to opt into hourly sender/subject/provider-snippet checking during awake hours (owner beta); `turn off email checks` stops it. IMPORTANT/starred labels and subject keywords guide conservative triage, with bounded provider snippet excerpts, not a full-content AI summary. Nothing goes to Gemini, no replies or actions follow email instructions, and free-host timing is best-effort. Quiet hours default21:00-09:00.

`/calendar_slot Title | ISO start with offset | ISO end with offset | IANA timezone` shows a private primary-calendar preview with Create/Cancel. Preview/Cancel live passed. Actual Create still needs optional `enable calendar booking` Google write-permission reconnect and owner approval. Optional explicit guest emails and popup reminder minutes; no Meet; conflicts rechecked, one-use review, no uncertain-write retry. No live event write claimed.

The Telegram command menu now includes browse/computer/research/CSV/chart/mail/calendar/updates controls. Auto-wake is enabled after real cold-start/worker/calculation/idle-stop proof. Public-form foundation is implemented; dynamic provider booking is not accepted.

### October 9 live acceptance and remaining gates

- Real Telegram work queue, dashboard, recorded completion, CSV and chart export passed. Silent WAV correctly reported no speech; blue video and PDF embedded code passed after LangChain changes.
- Tagged groups: anyone may request public search/news. Public lookup remains isolated. The2.35 group action route separately supports requester-owned actions after that member opts into the group audience; no ambient private history. News replies list dated index headlines and publishers, not independently verified article summaries. Real owner group news query returned dated headlines October 9 at 14:32; owner replied Fixed.
- Gemini stays primary. Optional OpenRouter fallback uses only `openrouter/free` with zero-price limits, no data collection, and supported-parameter requirement. Live forced-primary-failure fixture returned FALLBACK_READY. Automatic fallback enabled and deployed 14:41 IST; stored setting read back on. Gemini file URIs and non-image inline media are never forwarded to it.
- Calendar is owner-only for reads, reconnect, preview, Create and Cancel. `/calendar_slot Title | ISO start | ISO end | timezone | guests-or-none | popup-minutes-or-default` previews account, primary calendar, exact times, guests and notification policy. Guest invites fire only after the reviewed Create tap; account self is included. No live Create or invitation claimed. Reconnect via `enable calendar booking` before Create.
- Public form demo: `/public_form demo | {"name":"TEST ONLY","note":"controlled demo"}`. Ten-minute encrypted owner-only draft, page/field/destination fingerprint, exact payload, one-use approval, no uncertain retry. Only controlled demo is configured. No Google/Calendly dynamic-slot compatibility claim.
- Pending: Google write-scope reconnect and reviewed event test, docs final pixels, external Google public approval. Never pay for a security assessment without owner confirmation.

Latest 15:04 checkpoint: worker Playwright/Chromium and Debian browser libraries installed; direct controlled form inspection verified with zero POST. The real Telegram preview at 14:57 failed because a 14:56 idle-stop left a fresh cached heartbeat. Lifecycle fix clears readiness on accepted start/stop and serializes readiness/activity/enqueue against idle-stop. Full suite: 203 passed. Real owner Telegram preview4402 and screenshot4403 passed15:04 after cold-start. Owner approved the identical controlled demo15:12:54. Fresh preview4406 then one Submit returned receipt4408 and confirmation screenshot4409 at15:14. No provider booking/calendar/message/payment effect. Awake-but-exited worker needs a host restart or explicit supervisor start; API start on an already-awake host does not rerun postStart. Public-news topic/date/relevance/publisher cleanup is deployed, but post-fix group acceptance is still pending.

15:14 controlled-demo closeout: original owner approval inspected; identical TEST ONLY fields reviewed in fresh preview4406, clicked Submit once once, receipt4408 and screenshot4409 show Test form received at the configured destination. No real booking or calendar action. Calendar reconnect link prepared for owner, actual Create still requires a reviewed preview and approval.

Group action beta: @mention requests use each requester's own account. First use discloses the audience and requires that member's opt-in. Requested results/drafts are then visible to the whole group; only the requester can confirm their exact controls. Private history and pending private drafts are not carried into groups. Google connection and background alerts stay in DM. Email generation now writes a complete subject/body from the ask with supplied context, without invented facts; exact wording is preserved only when explicitly requested. Live group action acceptance remains to be checked.
