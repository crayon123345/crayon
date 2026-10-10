# Crayon connections, vault and computer plan

## Implemented foundation, not live consent

`connect workspace`, `connect github`, `status workspace`, `status github`,
`disconnect workspace`, `disconnect github` work in the member's private Telegram
chat. Link is a 10-minute single-use bearer credential: do not forward it.
Workspace uses a separate Google Cloud project/consent app and connection row, so Gmail/calendar keep
their existing college account. Account choice is explicit at Google consent.
Callbacks store encrypted token envelopes bound to user ID and provider. HTTP
access logs are disabled and failures return generic messages, not token content.
No model receives OAuth tokens. Delete-my-data removes both connection slots.

Workspace scopes: Docs and Sheets read/write plus verified email. These are
sensitive scopes. App test users, redirect URI and provider verification must be
configured before consent. No all-Drive restricted scope is requested. A separate Google Cloud project/consent
app is intentional: Google revocation must not disconnect the college app.
GitHub OAuth currently requests only read:user. It does not grant private-repo
access or write permissions. Selected private repos need a GitHub App with
Contents:read and Pull requests:read, plus installation identity binding and
short-lived installation tokens. Do not silently request broad repo scope.

`github digest owner/repo` reads a public repo's latest five commits and five
open PRs. Titles are untrusted source data, not instructions. Output is a direct
bounded listing, not a model summary. Thirty requests/member/day, no automatic
polling or rate-limit retries. No automatic forwarding to groups.

## Provider steps still needed

- Workspace separate Google Cloud project/consent app, Docs/Sheets APIs enabled, testing users and callback
  `CRAYON_PUBLIC_URL/connections/workspace/callback` registered. Client ID/secret
  enter server environment only, never chat or repository.
- GitHub OAuth app callback `CRAYON_PUBLIC_URL/connections/github/callback`.
  Credentials in server environment only. Public digest works without OAuth.
- Discord app + installation choice. Prefer signed slash-command HTTP
  interactions on the existing host, not message scraping or a user token.
  Verify timestamp and Ed25519 signature against exact raw body, persist unique
  interaction IDs, defer within provider deadline, bind buttons to requester
  and channel. Ephemeral responses for private information. Member IDs use a
  separate namespace; never merge identities by display name or guessed email.
- Implement bounded Workspace operations. Reads stay outside personal memory;
  AI summaries need an explicit document request. Writes preview exact target,
  account and content and require a requester/chat/hash-bound confirmation.
  Sheets writes use RAW to avoid formula injection by default. Native Docs
  revisions and Sheets target values must be read back before success claims.

## Credential vault threat model and gate

OAuth first. Passwords are not substitutes for provider OAuth where it exists.
A later vault needs HTTPS-only single-use owner-bound collection links, no
query-string secret values, CSP/no-referrer/no-store, no third-party scripts,
request body suppression and strict size/rate caps. Never collect secrets in
Telegram, Discord, WhatsApp, model context or general job JSON.

Use a separate rotating vault master key in server environment, encrypted
per-user data keys and authenticated encryption binding user+entry+purpose.
Encrypt usernames/labels as well as values. Database export alone cannot decrypt;
a compromised live service/master key can. This is not protection against a
server administrator. No plaintext secret retrieval endpoint. Browser workers
receive short-lived scoped fill capabilities through an authenticated encrypted
channel, never raw values in durable queues/results. No vault autoload into
models. Revoke capability after one operation; delete encrypted rows and report
backup-retention limits. Key rotation and restore tests precede public launch.

Vault endpoints are NOT shipped in this foundation. No new passwords collected.
Keep fail-closed until collector authentication, audit redaction, capability
transport, deletion and key rotation have passing adversarial tests.

## Computer isolation and free-only boundary

Existing computer is an owner-controlled bridge with limited tester public
browser/arithmetic jobs, quotas and no shell exposure. Do not expand it by
pointing all members at the owner's Codespace, browser cookies or saved logins.
A separate browser context is not an operating-system sandbox. Before arbitrary
code or saved credentials, require per-user disposable container/VM boundary,
egress policy blocking private networks/cloud metadata, disk/network/time caps,
no host sockets/mounts and trusted worker identity bound to tenant job ID.

Free resources may sleep and may have fixed allowance. No new paid VM, card,
upgrade or automatic charge. Use existing bounded bridge for owner's work while
researching a genuinely isolated free route. If none fits, leave general
multi-user computer access disabled and state the limitation plainly. Never
claim a dedicated computer has been provisioned from a code scaffold.

## Verification gates

Tests cover state expiry/replay/provider binding, encrypted user/provider
binding, narrow scope generation, existing Gmail/calendar isolation and invalid
repo input. Still needed: real callback/account readback, cross-tenant hostile
payloads, token revocation, real Workspace read/write previews, real Discord
signature/replay delivery, isolated computer lifecycle and secure vault flow.

Sources:
- https://developers.google.com/workspace/sheets/api/scopes
- https://developers.google.com/workspace/docs/api/auth
- https://docs.github.com/en/rest/commits/commits
- https://docs.github.com/en/rest/pulls/pulls
- https://docs.github.com/en/apps/oauth-apps/building-oauth-apps/authorizing-oauth-apps
- https://docs.discord.com/developers/quick-start/getting-started.md

- https://developers.google.com/identity/protocols/oauth2/cross-client-identity
