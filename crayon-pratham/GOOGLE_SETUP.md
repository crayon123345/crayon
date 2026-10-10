# Planned Google integration (M13, not connected)

The requested shape is user-agnostic: each Telegram user connects their own Google account. No OAuth flow or Google access is active yet.

## Architecture and safety requirements

- `/connect_google` creates a short-lived, random OAuth state bound to that Telegram user's ID and private chat. Never accept a user ID from the callback as authority.
- Google OAuth callback checks and consumes the exact state once, exchanges the code over TLS, and stores that user's tokens in Neon encrypted with a dedicated server-held key. Encryption key and OAuth client secret stay in server environment variables, never repository files or logs.
- Every Gmail/Calendar request resolves credentials by the authenticated Telegram user ID. Never share global tokens or expose another user's connection. Include a `/disconnect_google` command that revokes and deletes tokens plus pending email approvals.
- Read-only Gmail and Calendar features first. Minimize scopes; avoid full mailbox deletion or calendar edits.
- Sending is draft-first: show the exact Google From account, To/CC/BCC, subject and body. Require a draft-specific user confirmation, with expiry. Read the stored draft again and compare the content hash before sending. Changed drafts require a new confirmation. Model output, scheduled jobs and imported email content cannot authorize sends.
- Keep request caps and fail on quota errors. Do not attach billing, request quota increases or purchase assessments.
- Treat email/calendar content as untrusted data. Do not pass email content into permanent memory or training. Define retention and delete behavior before collecting other users' private data.
- End-to-end cross-user isolation, OAuth-state replay, token encryption, disconnect, approval expiry and draft tampering tests must pass before launch.

## Launch gate

Testing mode requires explicitly adding Google test users and has a 100-test-user limit. Gmail/Calendar refresh tokens issued in external Testing expire after seven days. It is not arbitrary-public-user onboarding.

Gmail readonly and compose are restricted scopes. A public app that stores or transmits restricted data through a server may need app verification and an annual independent security assessment. A free public deployment cannot be promised until that requirement is resolved. Personal-use exceptions do not automatically cover the requested public multi-user shape.

Standard API usage is currently at no additional cost. Google's docs say excess-quota billing is planned later in 2026. No billing setup or paid work is authorized.

## Official references

- https://developers.google.com/identity/protocols/oauth2/production-readiness/restricted-scope-verification
- https://developers.google.com/identity/protocols/oauth2
- https://developers.google.com/workspace/gmail/api/auth/scopes
- https://developers.google.com/workspace/gmail/api/reference/quota
- https://developers.google.com/workspace/calendar/api/guides/quota

Public rollout and verification preparation are requested, with a hard stop on any paid security assessment. Cloud ownership is routed to the personal Gmail account. No connection is active. Per-user OAuth/state/encryption code is being built and is not enabled. Developer contacts, actual privacy/data practices and working integration must be verified before any submission.
