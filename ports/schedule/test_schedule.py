"""Adversarial tests for /ports/schedule durable scheduled work.

Trait acceptance criteria under test (Dots scheduled work + check-in
ground layer):
- real job scheduler semantics: due jobs fire exactly once per tick
- inspectable run history (fires, deferrals, pauses, cancels, snoozes)
- pause/cancel/snooze; timezone-safe config; quiet hours; daily cap
- dedupe; defer-never-drop
Plus: cross-owner isolation, explicit grammar, caps. All clocks are
INJECTED (now=...) - the suite is fully deterministic. Every test counts
EFFECTS (runs, fires), not prose replies.
"""
import sys, os, json, unittest

sys.path.insert(0, os.path.dirname(__file__))
from schedule import (Schedule, MemScheduleStore, ScheduleError,
                      BadTimezone, JobLimitReached, MAX_JOBS_PER_OWNER,
                      MAX_FIRES_PER_DAY)

A, B = "v_owner_a", "v_owner_b"
# 2026-10-10 12:00:00 UTC (verified: 1791633600)
T0 = 1791633600
HOUR = 3600
MIN = 60


def fresh():
    return Schedule(MemScheduleStore())


class TestLifecycle(unittest.TestCase):
    def test_one_shot_fires_once_then_done(self):
        s = fresh()
        j, _ = s.add_job(A, "call mom", in_minutes=30, now=T0)
        assert s.tick(A, T0 + 29 * MIN) == []          # not yet due
        fired = s.tick(A, T0 + 30 * MIN)
        self.assertEqual([x.id for x in fired], [j.id])
        self.assertEqual(j.state, "done")
        self.assertEqual(j.fire_count, 1)
        self.assertEqual(s.tick(A, T0 + 31 * MIN), [])  # no double fire
        self.assertEqual(j.fire_count, 1)

    def test_interval_reschedules(self):
        s = fresh()
        j, _ = s.add_job(A, "stand up", in_minutes=60, interval_min=60,
                         now=T0)
        for k in (1, 2, 3):
            fired = s.tick(A, T0 + k * 60 * MIN)
            self.assertEqual(len(fired), 1)
        self.assertEqual(j.fire_count, 3)
        self.assertEqual(j.state, "active")
        self.assertEqual(j.next_fire, T0 + 4 * 60 * MIN)

    def test_pause_blocks_firing_resume_restores(self):
        s = fresh()
        j, _ = s.add_job(A, "call mom", in_minutes=10, now=T0)
        s.set_state(A, "mom", "paused", now=T0)
        self.assertEqual(s.tick(A, T0 + 11 * MIN), [])
        self.assertEqual(j.fire_count, 0)
        s.set_state(A, "mom", "active", now=T0 + 12 * MIN)
        self.assertEqual(len(s.tick(A, T0 + 13 * MIN)), 1)

    def test_cancel_never_fires(self):
        s = fresh()
        j, _ = s.add_job(A, "call mom", in_minutes=10, now=T0)
        s.set_state(A, "mom", "cancelled", now=T0)
        self.assertEqual(s.tick(A, T0 + 11 * MIN), [])
        self.assertEqual(j.state, "cancelled")

    def test_snooze_pushes_fire(self):
        s = fresh()
        j, _ = s.add_job(A, "call mom", in_minutes=10, now=T0)
        s.snooze(A, "mom", 45, now=T0)
        self.assertEqual(j.next_fire, T0 + 45 * MIN)
        self.assertEqual(s.tick(A, T0 + 10 * MIN), [])
        self.assertEqual(len(s.tick(A, T0 + 45 * MIN)), 1)
        # snooze on a done job refused, zero effects
        with self.assertRaises(ScheduleError):
            s.snooze(A, "mom", 5, now=T0 + 46 * MIN)

    def test_dedupe_key(self):
        s = fresh()
        j1, c1 = s.add_job(A, "check in", in_minutes=60, now=T0,
                           dedupe_key="followup f_123")
        j2, c2 = s.add_job(A, "check in again", in_minutes=120, now=T0,
                           dedupe_key="followup  f_123")
        self.assertTrue(c1)
        self.assertFalse(c2)                 # second not created
        self.assertEqual(j1.id, j2.id)
        self.assertEqual(s.store.count(A), 1)


class TestRunHistory(unittest.TestCase):
    def test_every_event_recorded(self):
        s = fresh()
        j, _ = s.add_job(A, "call mom", in_minutes=10, now=T0)
        s.snooze(A, "mom", 20, now=T0 + MIN)
        s.set_state(A, "mom", "paused", now=T0 + 2 * MIN)
        s.set_state(A, "mom", "active", now=T0 + 3 * MIN)
        s.tick(A, T0 + 21 * MIN)
        events = [r["event"] for r in j.runs]
        self.assertEqual(events, ["created", "snoozed", "paused",
                                  "resumed", "fired", "completed"])

    def test_history_command(self):
        s = fresh()
        j, _ = s.add_job(A, "call mom", in_minutes=10, now=T0)
        s.tick(A, T0 + 10 * MIN)
        reply = s.handle_message(A, "reminder mom history", now=T0)
        self.assertIn("fired", reply)


class TestQuietHoursAndCaps(unittest.TestCase):
    def test_invalid_tz_refused_zero_effects(self):
        s = fresh()
        with self.assertRaises(BadTimezone):
            s.set_timezone(A, "Mars/Olympus")
        self.assertEqual(s.store.get_tz(A), "UTC")   # unchanged

    def test_quiet_hours_defer_never_drop(self):
        s = fresh()
        s.set_timezone(A, "UTC")
        s.set_quiet_hours(A, 22, 7)
        # T0 is 12:00 UTC; make a job due at 23:00 UTC (inside quiet)
        j, _ = s.add_job(A, "late reminder", fire_at=T0 + 11 * HOUR, now=T0)
        fired = s.tick(A, T0 + 11 * HOUR)
        self.assertEqual(fired, [])                    # deferred
        self.assertEqual(j.runs[-1]["event"], "deferred_quiet")
        self.assertEqual(j.next_fire,
                         s._quiet_end_ts(A, T0 + 11 * HOUR))
        # fires after quiet ends (07:00 UTC next day)
        fired = s.tick(A, j.next_fire)
        self.assertEqual(len(fired), 1)
        self.assertEqual(j.fire_count, 1)

    def test_overnight_quiet_window_math(self):
        s = fresh()
        s.set_timezone(A, "UTC")
        s.set_quiet_hours(A, 22, 7)
        # 23:30 UTC is inside; 12:00 UTC is outside
        self.assertTrue(s._in_quiet(A, T0 + 11 * HOUR + 30 * MIN))
        self.assertFalse(s._in_quiet(A, T0))

    def test_daily_cap_defers_to_next_day(self):
        s = fresh()
        s.set_timezone(A, "UTC")
        # distinct fire times so the cap victim is deterministic
        # (equal next_fire would tie-break on random ids)
        for i in range(MAX_FIRES_PER_DAY):
            s.add_job(A, "job %d" % i, in_minutes=1 + i, now=T0)
        extra, _ = s.add_job(A, "one too many",
                             in_minutes=1 + MAX_FIRES_PER_DAY, now=T0)
        fired = s.tick(A, T0 + (1 + MAX_FIRES_PER_DAY) * MIN)
        self.assertEqual(len(fired), MAX_FIRES_PER_DAY)
        self.assertEqual(extra.state, "active")
        self.assertEqual(extra.runs[-1]["event"], "deferred_cap")
        # next day it fires
        fired = s.tick(A, extra.next_fire)
        self.assertEqual([x.id for x in fired], [extra.id])

    def test_quiet_hours_validation(self):
        s = fresh()
        with self.assertRaises(ScheduleError):
            s.set_quiet_hours(A, 25, 7)


class TestTimezoneSafety(unittest.TestCase):
    def test_absolute_time_in_owner_tz(self):
        s = fresh()
        s.set_timezone(A, "Asia/Calcutta")
        reply = s.handle_message(
            A, "remind me at 2026-10-11 09:00 to standup", now=T0)
        self.assertIn("09:00", reply)
        j = s.store.list(A)[0]
        # 09:00 IST = 03:30 UTC
        self.assertEqual(s._local(A, j.next_fire).hour, 9)

    def test_past_time_refused_zero_effects(self):
        s = fresh()
        s.set_timezone(A, "UTC")
        reply = s.handle_message(A, "remind me at 2020-01-01 09:00 to x",
                                 now=T0)
        self.assertIn("past", reply)
        self.assertEqual(s.store.count(A), 0)


class TestIsolationAndGrammar(unittest.TestCase):
    def test_cross_owner_zero_effects(self):
        s = fresh()
        s.add_job(A, "A's reminder", in_minutes=10, now=T0)
        s.set_timezone(B, "Asia/Calcutta")
        self.assertEqual(s.store.get_tz(A), "UTC")     # A unchanged
        reply = s.handle_message(B, "reminder A's reminder cancel",
                                 now=T0)
        self.assertIn("no reminder matches", reply)
        self.assertEqual(s.store.list(A)[0].state, "active")
        self.assertEqual(s.tick(B, T0 + 11 * MIN), [])  # B fires nothing
        self.assertEqual(len(s.export(B)["jobs"]), 0)

    def test_near_miss_phrasings_fall_through(self):
        s = fresh()
        for msg in ("remind me someday", "reminder", "remind me",
                    "quiet hours", "set the timezone please",
                    "what are my reminders about"):
            self.assertIsNone(s.handle_message(A, msg, now=T0), msg)
        self.assertEqual(s.store.count(A), 0)

    def test_read_commands_zero_effects(self):
        s = fresh()
        s.add_job(A, "x", in_minutes=10, now=T0)
        before = s.export(A)["jobs"]
        s.handle_message(A, "my reminders", now=T0)
        s.handle_message(A, "export my reminders", now=T0)
        self.assertEqual(s.export(A)["jobs"], before)

    def test_ambiguous_match_refused(self):
        s = fresh()
        s.add_job(A, "call mom monday", in_minutes=10, now=T0)
        s.add_job(A, "call mom tuesday", in_minutes=20, now=T0)
        reply = s.handle_message(A, "reminder mom cancel", now=T0)
        self.assertIn("more specific", reply)
        states = [j.state for j in s.store.list(A)]
        self.assertEqual(states, ["active", "active"])

    def test_job_cap_refuses(self):
        s = fresh()
        for i in range(MAX_JOBS_PER_OWNER):
            s.add_job(A, "job %d" % i, in_minutes=60 + i, now=T0)
        with self.assertRaises(JobLimitReached):
            s.add_job(A, "overflow", in_minutes=1, now=T0)
        self.assertEqual(s.store.count(A), MAX_JOBS_PER_OWNER)

    def test_export_shape(self):
        s = fresh()
        s.add_job(A, "x", in_minutes=10, now=T0)
        data = s.export(A)
        self.assertEqual(data["format"], "crayon-schedule-v1")
        self.assertIn("timezone", data)
        self.assertIn("runs", data["jobs"][0])
        json.dumps(data)


if __name__ == "__main__":
    unittest.main()


class Review5479xxxRegressionTests(unittest.TestCase):
    """Regression tests for PR #23 provisional review findings."""

    def setUp(self):
        self.s = Schedule(MemScheduleStore())
        self.A, self.B = "v_a", "v_b"

    def test_lowercase_timezone_accepted_and_canonicalized(self):
        self.s.set_timezone(self.A, "asia/calcutta")
        self.assertEqual(self.s.store.get_tz(self.A), "Asia/Calcutta")
        r = self.s.handle_message(self.A, "set timezone asia/calcutta",
                                  now=T0)
        self.assertIn("Asia/Calcutta", r)
        self.assertEqual(self.s.store.get_tz(self.A), "Asia/Calcutta")

    def test_unknown_timezone_still_refused_zero_effects(self):
        with self.assertRaises(BadTimezone):
            self.s.set_timezone(self.A, "asia/nowhere")
        with self.assertRaises(BadTimezone):
            self.s.set_timezone(self.A, "")
        self.assertEqual(self.s.store.get_tz(self.A), "UTC")

    def test_invalid_calendar_date_refused_zero_effects(self):
        for bad in ("2026-02-30", "2026-13-01", "2025-02-29", "2026-04-31"):
            r = self.s.handle_message(
                self.A, "remind me at %s 10:00 to x" % bad, now=T0)
            self.assertIn("does not exist", r)
        self.assertEqual(self.s.store.count(self.A), 0)

    def test_valid_date_and_time_still_schedule(self):
        r = self.s.handle_message(
            self.A, "remind me at 2026-10-11 09:30 to stretch", now=T0)
        self.assertIn("reminder set", r.lower())
        self.assertEqual(self.s.store.count(self.A), 1)

    def test_cancelled_and_done_jobs_do_not_count_toward_cap(self):
        # one early job so tick() completes exactly one job
        self.s.add_job(self.A, "early job", in_minutes=1, now=T0)
        for i in range(MAX_JOBS_PER_OWNER - 1):
            self.s.add_job(self.A, "job %d" % i, in_minutes=60, now=T0)
        with self.assertRaises(JobLimitReached):
            self.s.add_job(self.A, "overflow", in_minutes=60, now=T0)
        # completing a job frees a slot
        fired = self.s.tick(self.A, T0 + 60)
        self.assertEqual([j.text for j in fired], ["early job"])
        self.s.add_job(self.A, "replacement", in_minutes=60, now=T0)
        with self.assertRaises(JobLimitReached):
            self.s.add_job(self.A, "overflow 2", in_minutes=60, now=T0)
        # cancel frees a slot, as the refusal message promises
        self.s.set_state(self.A, "job 0", "cancelled", now=T0)
        self.s.add_job(self.A, "replacement 2", in_minutes=60, now=T0)
        # pause does NOT free a slot - a paused job is still live
        self.s.set_state(self.A, "job 48", "paused", now=T0)
        with self.assertRaises(JobLimitReached):
            self.s.add_job(self.A, "overflow 3", in_minutes=60, now=T0)

    def test_completed_one_shot_cannot_be_resumed_or_refire(self):
        self.s.add_job(self.A, "done job", in_minutes=1, now=T0)
        fired = self.s.tick(self.A, T0 + 60)
        self.assertEqual([j.text for j in fired], ["done job"])
        for st in ("active", "paused", "cancelled"):
            with self.assertRaises(ScheduleError):
                self.s.set_state(self.A, "done job", st, now=T0 + 120)
        # no refire even across many later ticks
        for k in range(50):
            self.assertEqual(self.s.tick(self.A, T0 + 120 + k * 60), [])
        # snooze already refuses non-active jobs; confirm on done
        with self.assertRaises(ScheduleError):
            self.s.snooze(self.A, "done job", 10, now=T0 + 120)

    def test_cancelled_is_terminal_and_resume_cap_guarded(self):
        # round-2 review 5479287218: cancel+create+resume bypass
        for i in range(MAX_JOBS_PER_OWNER):
            self.s.add_job(self.A, "capjob %02d" % i, in_minutes=60,
                           now=T0)
        self.s.set_state(self.A, "capjob 00", "cancelled", now=T0)
        self.s.add_job(self.A, "cap replacement", in_minutes=60, now=T0)
        # 50 live again; the cancelled job must be terminal, not resumable
        for st in ("active", "paused", "cancelled"):
            with self.assertRaises(ScheduleError):
                self.s.set_state(self.A, "capjob 00", st, now=T0 + 1)
        job = [j for j in self.s.store.list(self.A)
               if j.text == "capjob 00"][0]
        self.assertEqual(job.state, "cancelled")
        self.assertEqual(len(self.s.store.list(
            self.A, states=("active", "paused"))), MAX_JOBS_PER_OWNER)
        # and it never refires
        for k in range(20):
            self.assertNotIn("capjob 00",
                             [j.text for j in
                              self.s.tick(self.A, T0 + 3600 + k)])

    def test_cancelled_terminal_even_with_room(self):
        self.s.add_job(self.A, "dead job", in_minutes=60, now=T0)
        self.s.set_state(self.A, "dead job", "cancelled", now=T0)
        for st in ("active", "paused"):
            with self.assertRaises(ScheduleError):
                self.s.set_state(self.A, "dead job", st, now=T0 + 1)

    def test_resume_paused_at_full_cap_not_falsely_refused(self):
        # paused jobs are live: 49 active + 1 paused = cap; resuming the
        # paused one must succeed (no count change, no bypass)
        self.s.add_job(self.A, "capjob paused", in_minutes=60, now=T0)
        for i in range(MAX_JOBS_PER_OWNER - 1):
            self.s.add_job(self.A, "capjob filler %02d" % i,
                           in_minutes=60, now=T0)
        self.s.set_state(self.A, "capjob paused", "paused", now=T0)
        with self.assertRaises(JobLimitReached):
            self.s.add_job(self.A, "would be 51st live", in_minutes=60,
                           now=T0)
        j = self.s.set_state(self.A, "capjob paused", "active", now=T0)
        self.assertEqual(j.state, "active")
