# Crayon computer beta

Owner and one approved tester, one bounded 2-core Codespace. No public execution ports or imported browser cookies.

## Setup

1. Check included quota and the $0 paid budget before creating or starting a machine. Never raise paid budgets.
2. Store a dedicated bridge credential in Render's CRAYON_BRIDGE_TOKEN and the Codespace's ~/.config/crayon/bridge-token with mode 600. Never commit, print or send it.
3. In the Codespace: python3 -m pip install --user playwright==1.58.0, then python3 -m playwright install --with-deps chromium. Browser launch needs the Debian libraries too, not only downloaded Chromium. If package signature checks fail, fix the package source; never disable verification.
4. Configure autostart on in ~/.config/crayon/autostart or the deployment environment. The fixed postStart hook runs computer_start.sh, which launches the bounded supervisor and worker outbound to Render.
5. Verify the private bridge heartbeat and actual worker revision. A source change in GitHub does not update an already-running checkout.

## Lifecycle

Automatic host cold-start, worker readiness, calculation and idle-stop passed October 9. It uses one existing machine and a least-privilege lifecycle token held in the server environment. No token, quota or budget value belongs in this file.

A later real preview exposed a stale heartbeat after accepted stop. The fix invalidates readiness on accepted lifecycle changes and serializes readiness/activity/enqueue against idle-stop. Real controlled form preview and screenshot passed at 15:04 after cold-start. No live Submit is claimed.

The worker defaults to a 25-minute session. It exits instead of running forever. Calling the host start API on a host already awake does not rerun postStart. If the worker exited while the host stays awake, explicitly start the fixed supervisor or restart the host after checking there are no jobs. Do not claim this case automatically recovers.

Idle-stop is requested after ten minutes without bot activity and no pending/running jobs. Direct IDE/terminal activity is not bot activity and can be interrupted by idle-stop. Codespaces storage still uses allowance while stopped. Quota and pricing can change. Stop when finished; keep paid usage blocked.

## Operations and boundaries

- Basic arithmetic without eval, imports or shell jobs.
- Owner-only text files under ~/crayon-files, plain names, no overwrite/delete/symlinks.
- Approved testers: public browser/arithmetic only. Private owner files and form actions excluded.
- Public Chromium tasks: fresh context, HTTPS/public-IP/sensitive-portal checks, two pages maximum, GET-only, no account cookies/login/purchases/downloads/arbitrary scripts. A viewport screenshot proves what was displayed, not source accuracy.
- Separate owner-only public-form adapter: exact configured free HTML form, encrypted ten-minute preview, page/destination/field fingerprint, fresh check, one approved POST, exact payload and confirmation receipt. Controlled demo only by default. Never treat a generic receipt as a venue reservation. Google appointment/Calendly dynamic slots are not accepted. No login/payment/fees or uncertain automatic retry.

Five jobs per tester/day, twenty owner/day, thirty total/day, atomically reserved including failed jobs. No extra testers without verified IDs. Transient job text/screenshots are removed after thirty minutes on the next worker poll; stopped workers do not run cleanup. Screenshots are not personal memory.

203 local tests pass. Actual owner Telegram queue/media/export/mail/form-preview proof is recorded in TEST_CHECKLIST.md. Outside-tester transport and live calendar Create/guest delivery remain separate gates.
