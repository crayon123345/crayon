# Canonical Crayon Discord pilot

Status: implementation for cross-review, NOT a live bot and NOT full parity accepted.
No existing live service is changed. The public text-only `crayon-web/discord_bot.py`
and `integrated/public/discord_bot.py` are intentionally untouched.

## Source and architecture

`integrated/private/cr_discord.py` is a transport over the same private engine used
by the integrated authenticated app and full Telegram source. It calls the actual
command handlers, tool/review paths and media analyzer, not a new model-only chat.
The dedicated runner does not poll Telegram, start WhatsApp or run the public
engine. It has its own Discord account mapping and pilot database. No source
Telegram IDs, credentials, user history, owner privileges or databases are copied.

Comparison baseline: team main `fec27a7b23d3d3e4a004c4a4b7cf8b1f13fdbd1a`,
`integrated/private/cr_telegram.py`, `cr_agent.py`, `cr_media.py`, `cr_sched.py`,
`cr_work.py`, `cr_google_chat.py`, `cr_web_app.py` and `cr_web_actions.py`.
Personal release `itsppm76/crayon` at `ab7412d7` remains deployed separately.
The source baselines are not identical; this is not a production cutover.

## Capability comparison

"Wired" below means a real shared-engine code path with synthetic tests, not
Discord/provider live acceptance. Every live row remains pending until the app,
token, approved test accounts and free host are configured and tested.

| Capability | Telegram/web baseline | Discord pilot | Remaining acceptance |
| --- | --- | --- | --- |
| Ordinary chat and tools | Full private engine | Wired to same handlers, not tool-block stripping | Actual Gateway roundtrip and model/tool result |
| Provider fallback | Canonical Gemini/OpenRouter policy | Inherited unchanged, no new provider/key | Force quota/empty/hard-error on this host; no paid fallback |
| Memory, notes, tasks, timezone | Per canonical account | New isolated Discord account per authenticated Discord subject | DB persistence/restart, cross-user isolation |
| Existing account/history sharing | Telegram/web explicit identity/link paths | NOT implemented | Two-sided proof and exact linking review; never infer by username/email |
| Gmail/calendar/Workspace reads | Own OAuth account, non-model private path | Wired through private DM commands | Real test account OAuth and private read; Google tester limits still apply |
| Email/calendar/Workspace/form effects | Exact reviewed drafts and controls | Requester + DM channel + control message bound, one-use, 10-minute buttons | Live preview/cancel, changed/expired/cross-user controls; actual effects only on approved fixtures |
| Attachments, images, audio/video/doc analysis | Bounded media analyzer, 20 MB input | Same analyzer; Discord CDN-only single-file input | Actual photo/PDF/audio/video upload; byte/redirect failures |
| Generated CSV/chart/files/audio | Existing engine/provider gates | Native Discord file upload, canonical 2 MB output limit | Receive and inspect actual attachment; provider free-tier gates |
| Reminder/scheduled read-only job | In-process scheduler | Explicit per-Discord-user background loop | Near-future reminder receipt, restart, asleep/late/failed-DM behavior |
| Opt-in digests/check-ins/mail metadata watch | Quiet-hour, account-bound, best effort | Same paths, private Discord destination | Actual opt-in/quiet-hours/delivery and disable |
| Internal work queue/control/export | Canonical work engine | Wired, same exact owner controls | Job completion and TXT export receipt |
| Natural-language + typed commands | Telegram/web command router | Wired; `/crayon text` native app command also registered | Actual application installation and command invocation |
| Mentions in shared channels | Telegram public route and per-member personal-action opt-in | Allowlisted-server public-only mentions | Actual mention/no-mention tests; personal server actions NOT implemented |
| Web shared rooms | Explicit reviewed audience in web | NOT mirrored/bridged | Discord-native private audience/opt-in design and tests |
| MCP/user-connected reviewed tools | Web-specific review UI | NOT implemented in Discord | Canonical review bridge, not arbitrary tool execution |
| Voice notes/read-aloud | Explicit free provider mode/device speech | Audio attachments use existing explicit voice path | Live audio receipt; no browser device TTS on Discord |
| Voice-channel calls/live conversation | Web voice still gated, no Telegram equivalent | NOT implemented | Separate consent/provider/free-quota work |
| Export formats/history UI | Web PDF/DOCX/MD/PPTX and history UI | Engine-generated files and work TXT export only | Native full-history export/menu parity NOT implemented |
| Delete/forget | Canonical reviewed erasure | Reviewed delete and per-fact forget; Discord routing/control/inbox records included | Real DB erasure + late-worker race proof; Discord's own message retention remains separate |
| Reactions/message deletion | Telegram API support | Optional reactions disabled; bot cannot delete another user's DM message | No false success/deletion claim; user removes their own secret message |

This matrix deliberately exposes missing parity. Do not label this pilot "same
everything" or "fully live" based on source tests.

## Free-only setup gates

1. Obtain cross-side review and Uttiya's exact merge approval. No direct main push.
2. Create/choose the user-approved Discord application. Store its bot token only
   through the vault and server environment. Never put it in a commit, URL or chat.
3. Enable Message Content intent in the Developer Portal for ordinary DMs/mentions.
   The bot uses Gateway authentication, not an interaction webhook endpoint.
4. Install with `bot` and `applications.commands`. Minimal server permissions:
   Send Messages, Attach Files, Read Message History, Add Reactions if later enabled.
   No Administrator, Manage Guild or Manage Messages permission is requested.
5. Set verified pilot user IDs and optional pilot guild IDs, not usernames. DMs
   disabled by a user are a real delivery blocker, not permission to post privately
   intended answers in a server. `/crayon` responses use the user's private DM.
6. Select an approved free process host and separate free pilot database. This PR
   does not authorize a paid plan, an extra billable service, production DB reuse
   or changing the existing Render service. Free hosts may sleep/disconnect, and
   a Gateway bot needs a running process; reminder timing is best effort.
7. Provision canonical provider/config/OAuth values through the existing secure
   server process. No new provider is selected and no paid tier/overage enabled.

Environment (values intentionally absent):

```
CRAYON_DISCORD_ENABLED=on
CRAYON_DISCORD_PILOT_DATABASE=confirmed-isolated
DISCORD_BOT_TOKEN=<vault-managed>
CRAYON_DISCORD_ALLOWED_USERS=<verified comma-separated numeric IDs>
CRAYON_DISCORD_ALLOWED_GUILDS=<optional verified comma-separated numeric IDs>
DATABASE_URL=<isolated pilot canonical database>
```

The existing canonical engine additionally needs its configured encryption,
provider and OAuth settings. Reuse its documentation; do not copy production
credentials or user data to a different environment without approval.

From `ports/discord`, install `requirements.txt`, then `python run.py`. No token,
allowlist, isolated-database confirmation or DB => refuse startup. The `discord.py`
package version used in local client-construction tests was 2.7.1.

## Safety and recovery

- Ignore bots/webhooks and all nonallowlisted accounts. Server replies require
  an explicit bot mention in an allowlisted guild and never use personal memory.
- 8 inbound requests/minute per Discord user, 4 concurrent private engine requests,
  maximum 16 pending, 4,000-character input, 1,900 UTF-16-unit reply chunks.
- Discord IDs never become Telegram IDs. Canonical DB allocates account IDs with
  the existing standalone account sequence. No owner privilege is inferred.
- All API sends set `allowed_mentions` to none. No `@everyone` or user pings from
  model text, sources, filenames or reply defaults.
- Every provider write gets one attempt. An uncertain inbox request or review is
  never automatically replayed. No effect based on an unshown/partial review.
- Controls contain opaque random IDs, not raw review hashes/account data. They
  are stored encrypted and bound to owner, DM channel and exact control message.
  Full review text is sent before the controls can become active. Expiry is
  checked by the database at consumption. A claim is atomic and one use only.
- Uploaded files allow only Discord attachment CDN HTTPS URLs, no redirects,
  and enforce both advertised and actual byte caps. No raw files kept in history.
- Bot cannot delete your DM message. Secret rejection says this honestly rather
  than claiming the message was removed.
- Token absent/feature flag off => dormant. No global monkeypatch or Telegram
  consumer is used. Do not run two Discord consumers for one bot/database.

## Verification

From `integrated/private`: `python -m pytest -q`.
Current full-suite result: 709 passed, including 34 new Discord synthetic tests.
Tests cover ID namespace, feature/allowlist gates, disabled-route isolation,
UTF-16 limits, no pings, no retries, review audience/expiry/one-shot SQL,
partial-review failure, no false deletion/reactions, account cleanup, dedupe,
recovery, rate cap, attachment CDN/size validation, input cap and quote audience.
This does not prove live Discord authentication, real DB races, OAuth, provider
access, delivery, exact effect behavior or hosting continuity. App and real
controlled acceptance are still required.

Official setup/constraint references checked 2026-10-17:
- https://docs.discord.com/developers/quick-start/getting-started
- https://docs.discord.com/developers/topics/gateway
- https://docs.discord.com/developers/resources/message
