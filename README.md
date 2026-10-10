# Crayon

Joint AI chatbot project by Uttiya Biswas and Pratham Prateek Mohanty.

## Repository layout

- `crayon-web/` - Uttiya's Crayon-Web source (live at https://crayon-web.onrender.com)
- `crayon-pratham/` - Pratham's source (import pending)

Root stays clean: this README and shared docs only. All app code lives in folders.

## Workflow

- No direct pushes to `main`. All changes land via pull requests.
- Cross-side review: Uttiya's side reviews Pratham's PRs and vice versa.
- A PR merges after one cross-side approval.
- Live services stay separate until a joint cutover decision.

## Ownership lanes (proposed)

- Uttiya's side: design/UX + history polish, read-only sharing, browser Pyodide, voice UX, agent mode, image generation, WhatsApp (Twilio) adapter, follow-up chips, live data cards, search focus modes.
- Pratham's side: scheduling/notification center, private briefings, reviewed Gmail/calendar/Workspace flows, acceptance ledger, plain-language controls, consent-led Drive discovery, isolation/retention review.

## First joint milestone (free pilot)

1. One freshly verified scheduled reminder
2. One private briefing
3. Explicit Create/Send acceptance
4. Delivery readback
