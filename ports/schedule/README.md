# /ports/schedule - durable scheduled work with inspectable runs

Companion-trait build (OpenAI Dots "scheduled work") and the ground layer
for contextual check-ins: durable per-user jobs with pause/cancel/snooze,
timezone-safe quiet hours, daily fire caps, dedupe, and an INSPECTABLE
run history. Stands alone.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

This port is complete as a tested module (21/21 tests) but is NOT wired
into the running app. Named limits, exactly:

1. NOT MOUNTED and NO DELIVERY: this module owns schedule STATE and due
   computation - tick(owner, now) returns the jobs that fired; DELIVERY
   (chat push / WhatsApp) is the integration's job, and the WhatsApp
   adapter is still a disabled scaffold. Nothing here sends anything,
   ever. There is no clock loop; the integration calls tick().
2. RUNS ARE INSPECTABLE: every created/fired/completed/deferred_quiet/
   deferred_cap/paused/resumed/cancelled/snoozed event lands in the
   job's run history (capped at 100) with a reason - no silent behavior.
3. DEFER, NEVER DROP: a fire inside quiet hours moves to quiet end; a
   fire past the daily cap (MAX_FIRES_PER_DAY = 10 per owner) moves to
   the next local midnight. Both deferrals are recorded.
4. TIMEZONE-SAFE: quiet hours, daily caps, and absolute-time commands
   compute in the owner's declared IANA timezone (zoneinfo). Default is
   UTC and the default is documented to the user in replies. An invalid
   timezone is refused with zero effects.
5. DEDUPE: add_job with an active/paused job of the same normalized
   dedupe key returns the existing job instead of creating a second.
6. CAPS REFUSE, NEVER EVICT: MAX_JOBS_PER_OWNER = 50; the 51st raises.
7. CLOCKS ARE INJECTED: every time-dependent method takes `now`; the
   module reads the wall clock only when the integration omits it. The
   test suite is fully deterministic.
8. STORAGE IS INJECTED, NOT DURABLE: MemScheduleStore is in-memory; the
   SQL adapter (same encryption-at-rest and vid keying convention as
   cw_mem) is integration work, deliberately not shipped half-wired.
9. CHECK-IN WIRING IS NOT HERE: kind="checkin" jobs exist, but linking
   them to goals/followups (the P1 #4 trait) is the check-in port's
   composition work; this module is the scheduler underneath it.

## Commands (chat surface)

- `remind me in <N> <minutes|hours> to <text>`
- `remind me at YYYY-MM-DD HH:MM to <text>` (owner's timezone)
- `my reminders` - active + paused with next fire times
- `reminder <match> pause|resume|cancel|snooze <N> <minutes|hours>`
- `reminder <match> history` - the inspectable run log
- `set timezone <IANA name>`
- `quiet hours <start> to <end>` / `quiet hours off` (local hours)
- `export my reminders` - JSON (crayon-schedule-v1) incl. run history

## Acceptance criteria covered by tests (test_schedule.py, 21 tests)

- One-shot jobs fire exactly once then complete; interval jobs
  reschedule; no double-fires; paused/cancelled jobs never fire; snooze
  pushes the fire time and refuses on non-active jobs.
- Inspectability: every lifecycle event recorded in order; history
  command renders it.
- Quiet hours: overnight-window math correct; due-in-quiet jobs defer to
  quiet end (recorded) and fire after; invalid hours refused.
- Daily cap: the (cap+1)th fire in a local day defers to next midnight
  (recorded) and fires the next day.
- Timezone safety: absolute times interpreted in the owner's tz; past
  times refused with zero effects; invalid tz refused with zero effects.
- Dedupe by normalized key; ambiguous matches refused with zero effects.
- No cross-owner leakage: second owner's timezone, commands, ticks and
  exports have zero effect on the first owner.
- Grammar discipline: near-miss phrasings fall through; read-only
  commands create zero effects; 51st job refused.
- Determinism: all clocks injected; suite repeats identically.

## Version

crayon-schedule-v1
