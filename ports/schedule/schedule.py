"""Schedule - durable per-user scheduled work with inspectable runs.

Companion-trait build (OpenAI Dots "scheduled work": real job scheduler
with inspectable run history, pause/cancel, timezone-safe config) and the
ground layer for P1 #4 contextual check-ins (opt-in, quiet hours,
frequency caps, snooze, dedupe).

Design rules (same discipline as the other ports):
- Every operation takes `owner` explicitly; no ambient identity, no
  cross-owner path exists in this module.
- STATE + DUE COMPUTATION ONLY: tick(now) returns due jobs and records
  runs. DELIVERY (WhatsApp/chat push) is an integration point and is NOT
  here; this module never sends anything and never loops on a clock.
- RUNS ARE INSPECTABLE: every fire, deferral (quiet hours, daily cap),
  pause, resume, cancel and snooze lands in the job's run history with a
  reason. No silent behavior.
- TIMEZONE-SAFE: quiet hours and daily caps are computed in the owner's
  declared IANA timezone (zoneinfo); an invalid timezone is refused with
  zero effects. UTC is the default and is documented.
- DEFER, NEVER DROP: a fire inside quiet hours or past the daily cap is
  deferred (to quiet end / next local day) and the deferral is recorded -
  reminders are never silently skipped.
- DEDUPE: an active job with the same dedupe key is not created twice.
- Caps refuse, never evict (jobs per owner).
- Storage is injected (ScheduleStore). MemScheduleStore is NOT durable;
  the SQL adapter is the integration point named in the README.
"""

import re
import time
import secrets
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

JOB_KINDS = ("reminder", "checkin")
JOB_STATES = ("active", "paused", "cancelled", "done")
MAX_JOBS_PER_OWNER = 50
MAX_TEXT = 300
MAX_RUN_HISTORY = 100
MAX_FIRES_PER_DAY = 10        # per owner, across all jobs (quiet discipline)
DEFAULT_QUIET = None          # (start_hour, end_hour) local, e.g. (22, 7)
SNOOZE_MAX_MINUTES = 24 * 60

EXPORT_FORMAT = "crayon-schedule-v1"


class ScheduleError(Exception):
    pass


class BadTimezone(ScheduleError):
    pass


class JobLimitReached(ScheduleError):
    pass


def _norm(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


_tz_canon_cache = None


def canonical_tz_name(name):
    """Return the canonical-cased IANA name for NAME, case-insensitively.

    Users type "asia/calcutta"; the zoneinfo database is case-sensitive.
    Match case-insensitively and hand back the canonical casing so stored
    settings and rendered replies always carry the real IANA name.
    Raises BadTimezone with zero effects when nothing matches.
    """
    global _tz_canon_cache
    try:
        return ZoneInfo(name) and name  # fast path: already canonical
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    if _tz_canon_cache is None:
        from zoneinfo import available_timezones
        _tz_canon_cache = {n.lower(): n for n in available_timezones()}
    canon = _tz_canon_cache.get((name or "").lower())
    if canon is None:
        raise BadTimezone("unknown timezone %r (use an IANA name like "
                          "Asia/Calcutta)" % name)
    return canon


def valid_tz(name):
    return ZoneInfo(canonical_tz_name(name))


class Job:
    __slots__ = ("id", "owner", "kind", "text", "interval_min", "state",
                 "next_fire", "last_fired", "fire_count", "dedupe_key",
                 "runs", "created")

    def __init__(self, owner, kind, text, next_fire, interval_min=None,
                 dedupe_key=None, jid=None, state="active", last_fired=None,
                 fire_count=0, runs=None, created=None):
        self.id = jid or "j_" + secrets.token_hex(8)
        self.owner = owner
        self.kind = kind if kind in JOB_KINDS else "reminder"
        self.text = (text or "")[:MAX_TEXT]
        self.interval_min = interval_min  # None = one-shot
        self.state = state if state in JOB_STATES else "active"
        self.next_fire = int(next_fire)
        self.last_fired = last_fired
        self.fire_count = int(fire_count)
        self.dedupe_key = _norm(dedupe_key) if dedupe_key else None
        self.runs = list(runs or [])
        self.created = int(created if created is not None else time.time())

    def note(self, ts, event, detail=""):
        self.runs.append({"ts": int(ts), "event": event, "detail": detail})
        if len(self.runs) > MAX_RUN_HISTORY:
            self.runs = self.runs[-MAX_RUN_HISTORY:]

    def to_dict(self):
        return {"id": self.id, "kind": self.kind, "text": self.text,
                "state": self.state, "interval_min": self.interval_min,
                "next_fire": self.next_fire, "last_fired": self.last_fired,
                "fire_count": self.fire_count,
                "dedupe_key": self.dedupe_key, "runs": list(self.runs),
                "created": self.created}


class MemScheduleStore:
    """In-memory store. NOT durable - tests and staging only."""

    def __init__(self):
        self._jobs = {}
        self._tz = {}         # owner -> IANA tz name
        self._quiet = {}      # owner -> (start_hour, end_hour) or None

    def put(self, job):
        self._jobs[job.id] = job

    def get(self, owner, jid):
        j = self._jobs.get(jid)
        return j if j and j.owner == owner else None

    def list(self, owner, states=None):
        out = [j for j in self._jobs.values() if j.owner == owner]
        if states:
            out = [j for j in out if j.state in states]
        return sorted(out, key=lambda j: (j.next_fire, j.id))

    def count(self, owner):
        return sum(1 for j in self._jobs.values() if j.owner == owner)

    def delete(self, owner, jid):
        j = self.get(owner, jid)
        if j is None:
            return False
        del self._jobs[jid]
        return True

    def get_tz(self, owner):
        return self._tz.get(owner, "UTC")

    def set_tz(self, owner, name):
        self._tz[owner] = canonical_tz_name(name)  # raises BadTimezone

    def get_quiet(self, owner):
        return self._quiet.get(owner, DEFAULT_QUIET)

    def set_quiet(self, owner, hours):
        self._quiet[owner] = hours


class Schedule:
    """Durable per-owner scheduled jobs with inspectable run history."""

    def __init__(self, store):
        self.store = store

    # ---------- job lifecycle ----------

    def add_job(self, owner, text, fire_at=None, in_minutes=None,
                interval_min=None, kind="reminder", dedupe_key=None,
                now=None):
        now = int(now if now is not None else time.time())
        text = (text or "").strip()[:MAX_TEXT]
        if not text:
            raise ScheduleError("empty job text")
        if fire_at is None:
            if not in_minutes or in_minutes <= 0:
                raise ScheduleError("need a fire time or a positive "
                                    "in_minutes")
            fire_at = now + int(in_minutes) * 60
        if interval_min is not None and interval_min <= 0:
            raise ScheduleError("interval must be positive minutes")
        dk = _norm(dedupe_key) if dedupe_key else None
        if dk:
            for j in self.store.list(owner, states=("active", "paused")):
                if j.dedupe_key == dk:
                    return j, False            # dedupe: already exists
        live = len(self.store.list(owner, states=("active", "paused")))
        if live >= MAX_JOBS_PER_OWNER:
            raise JobLimitReached(
                "schedule is full (%d active/paused jobs); cancel some "
                "first - cancelled and completed jobs do not count"
                % MAX_JOBS_PER_OWNER)
        j = Job(owner, kind, text, int(fire_at), interval_min=interval_min,
                dedupe_key=dedupe_key)
        j.note(now, "created", "fires at %d" % j.next_fire)
        self.store.put(j)
        return j, True

    def _match_job(self, owner, match):
        match_n = _norm(match)
        if not match_n:
            raise ScheduleError("empty reminder match")
        cands = [j for j in self.store.list(owner)
                 if match_n in _norm(j.text) or match_n == j.id]
        if not cands:
            raise ScheduleError("no reminder matches %r" % match)
        if len(cands) > 1:
            raise ScheduleError("several reminders match - be more "
                                "specific: "
                                + "; ".join(j.text[:40] for j in cands[:5]))
        return cands[0]

    def set_state(self, owner, match, state, now=None):
        if state not in ("paused", "active", "cancelled"):
            raise ScheduleError("state must be pause/resume/cancel")
        now = int(now if now is not None else time.time())
        j = self._match_job(owner, match)
        if j.state == "done":
            raise ScheduleError(
                "reminder already completed - it cannot be "
                "paused, resumed or cancelled")
        j.state = state
        j.note(now, {"paused": "paused", "active": "resumed",
                     "cancelled": "cancelled"}[state])
        self.store.put(j)
        return j

    def snooze(self, owner, match, minutes, now=None):
        now = int(now if now is not None else time.time())
        if not minutes or minutes <= 0 or minutes > SNOOZE_MAX_MINUTES:
            raise ScheduleError("snooze must be 1..%d minutes"
                                % SNOOZE_MAX_MINUTES)
        j = self._match_job(owner, match)
        if j.state != "active":
            raise ScheduleError("only an active reminder can be snoozed")
        j.next_fire = now + int(minutes) * 60
        j.note(now, "snoozed", "%d minutes" % minutes)
        self.store.put(j)
        return j

    # ---------- timezone + quiet hours ----------

    def set_timezone(self, owner, name):
        self.store.set_tz(owner, name)  # raises BadTimezone, zero effects

    def set_quiet_hours(self, owner, start_hour, end_hour):
        for h in (start_hour, end_hour):
            if not (0 <= h <= 23):
                raise ScheduleError("quiet hours must be 0..23")
        self.store.set_quiet(owner, (start_hour, end_hour))

    # ---------- due computation (pure in `now`) ----------

    def _local(self, owner, ts):
        return datetime.fromtimestamp(ts, tz=timezone.utc).astimezone(
            valid_tz(self.store.get_tz(owner)))

    def _in_quiet(self, owner, ts):
        q = self.store.get_quiet(owner)
        if not q:
            return False
        start, end = q
        h = self._local(owner, ts).hour
        return (h >= start or h < end) if start > end else \
            (start <= h < end)

    def _quiet_end_ts(self, owner, ts):
        """Next local time the owner is outside quiet hours."""
        start, end = self.store.get_quiet(owner)
        loc = self._local(owner, ts)
        cand = loc.replace(hour=end, minute=0, second=0, microsecond=0)
        while cand.astimezone(timezone.utc).timestamp() <= ts or \
                self._in_quiet(owner, int(cand.astimezone(
                        timezone.utc).timestamp())):
            cand += timedelta(hours=1)
            cand = cand.replace(minute=0, second=0, microsecond=0)
            if (cand - loc) > timedelta(days=2):
                break
        return int(cand.astimezone(timezone.utc).timestamp())

    def _fires_today(self, owner, now):
        loc_today = self._local(owner, now).date()
        n = 0
        for j in self.store.list(owner):
            for r in j.runs:
                if r["event"] == "fired" and \
                        self._local(owner, r["ts"]).date() == loc_today:
                    n += 1
        return n

    def _next_local_midnight(self, owner, now):
        loc = self._local(owner, now)
        nxt = (loc + timedelta(days=1)).replace(hour=0, minute=0,
                                                second=0, microsecond=0)
        return int(nxt.astimezone(timezone.utc).timestamp())

    def tick(self, owner, now=None):
        """Fire due jobs for `owner`. Returns the jobs that fired THIS
        tick (caller delivers them). Deferrals (quiet hours, daily cap)
        are recorded in run history and push next_fire - nothing is
        dropped silently. Interval jobs reschedule; one-shots complete."""
        now = int(now if now is not None else time.time())
        fired = []
        for j in self.store.list(owner, states=("active",)):
            if j.next_fire > now:
                continue
            if self._in_quiet(owner, now):
                qe = self._quiet_end_ts(owner, now)
                j.next_fire = qe
                j.note(now, "deferred_quiet", "next fire %d" % qe)
                self.store.put(j)
                continue
            if self._fires_today(owner, now) >= MAX_FIRES_PER_DAY:
                nm = self._next_local_midnight(owner, now)
                j.next_fire = nm
                j.note(now, "deferred_cap", "daily cap %d reached; next "
                       "fire %d" % (MAX_FIRES_PER_DAY, nm))
                self.store.put(j)
                continue
            j.last_fired = now
            j.fire_count += 1
            j.note(now, "fired")
            if j.interval_min:
                j.next_fire = now + j.interval_min * 60
            else:
                j.state = "done"
                j.note(now, "completed", "one-shot")
            self.store.put(j)
            fired.append(j)
        return fired

    def export(self, owner):
        return {"jobs": [j.to_dict() for j in self.store.list(owner)],
                "timezone": self.store.get_tz(owner),
                "quiet_hours": self.store.get_quiet(owner),
                "exported_at": int(time.time()),
                "format": EXPORT_FORMAT}

    # ---------- chat command surface ----------

    def handle_message(self, owner, text, source_sid=None, source_ts=None,
                       now=None):
        """Parse a user chat message. Returns a reply string when the
        message was a schedule command (caller should NOT pass it to the
        model), else None. Grammar is explicit; ambiguous phrasing falls
        through. `now` is injected by the integration (testable)."""
        t = (text or "").strip()
        low = re.sub(r"\s+", " ", t.lower())
        now = int(now if now is not None else time.time())

        m = re.match(r"^remind me in (\d+)\s*(m|min|mins|minutes|h|hr|"
                     r"hours?)\s+(?:to\s+)?(.+)$", low)
        if m:
            n = int(m.group(1))
            minutes = n * 60 if m.group(2).startswith("h") else n
            try:
                j, created = self.add_job(owner, m.group(3),
                                          in_minutes=minutes, now=now)
            except (ScheduleError, JobLimitReached) as e:
                return str(e)
            return ("Reminder set for %s (id %s). 'my reminders' lists "
                    "it; 'reminder %s cancel' cancels."
                    % (self._local(owner, j.next_fire).strftime(
                        "%Y-%m-%d %H:%M %Z"), j.id, j.text[:20]))

        m = re.match(r"^remind me at (\d{4}-\d{2}-\d{2})[t ](\d{2}:\d{2})"
                     r"\s+(?:to\s+)?(.+)$", low)
        if m:
            tz = valid_tz(self.store.get_tz(owner))
            try:
                loc = datetime.strptime(m.group(1) + " " + m.group(2),
                                        "%Y-%m-%d %H:%M").replace(tzinfo=tz)
            except ValueError:
                return ("That date or time does not exist (%s %s) - "
                        "nothing scheduled." % (m.group(1), m.group(2)))
            ts = int(loc.astimezone(timezone.utc).timestamp())
            if ts <= now:
                return "That time is in the past (%s)." % \
                    loc.strftime("%Y-%m-%d %H:%M %Z")
            try:
                j, created = self.add_job(owner, m.group(3), fire_at=ts,
                                          now=now)
            except (ScheduleError, JobLimitReached) as e:
                return str(e)
            return "Reminder set for %s (id %s)." % (
                loc.strftime("%Y-%m-%d %H:%M %Z"), j.id)

        m = re.match(r"^reminder\s+(.+?)\s+(pause|resume|cancel)$", low)
        if m:
            try:
                j = self.set_state(owner, m.group(1),
                                   {"pause": "paused", "resume": "active",
                                    "cancel": "cancelled"}[m.group(2)],
                                   now=now)
            except ScheduleError as e:
                return str(e)
            return "Reminder '%s' %s." % (j.text[:50], j.state)

        m = re.match(r"^reminder\s+(.+?)\s+snooze\s+(\d+)\s*"
                     r"(m|min|mins|minutes|h|hr|hours?)$", low)
        if m:
            n = int(m.group(2))
            minutes = n * 60 if m.group(3).startswith("h") else n
            try:
                j = self.snooze(owner, m.group(1), minutes, now=now)
            except ScheduleError as e:
                return str(e)
            return "Snoozed '%s' to %s." % (
                j.text[:40],
                self._local(owner, j.next_fire).strftime("%H:%M %Z"))

        if re.match(r"^(my reminders|list reminders|reminders)$", low):
            jobs = [j for j in self.store.list(owner)
                    if j.state in ("active", "paused")]
            if not jobs:
                return "No active reminders."
            lines = []
            for j in jobs:
                lines.append("- [%s] %s - next %s" % (
                    j.state, j.text[:50],
                    self._local(owner, j.next_fire).strftime(
                        "%Y-%m-%d %H:%M %Z")))
            return "\n".join(lines)

        m = re.match(r"^(?:show )?reminder\s+(.+?)\s+history$", low)
        if m:
            try:
                j = self._match_job(owner, m.group(1))
            except ScheduleError as e:
                return str(e)
            if not j.runs:
                return "No runs recorded for '%s'." % j.text[:40]
            lines = ["History for '%s':" % j.text[:40]]
            for r in j.runs[-10:]:
                lines.append("- %s %s %s" % (
                    self._local(owner, r["ts"]).strftime("%m-%d %H:%M"),
                    r["event"], r["detail"]))
            return "\n".join(lines)

        m = re.match(r"^set timezone\s+([A-Za-z_/+-]+)$", low)
        if m:
            try:
                self.set_timezone(owner, m.group(1))
            except BadTimezone as e:
                return str(e)
            return "Timezone set to %s." % self.store.get_tz(owner)

        m = re.match(r"^quiet hours (\d{1,2})\s*(?:to|-)\s*(\d{1,2})$", low)
        if m:
            try:
                self.set_quiet_hours(owner, int(m.group(1)),
                                     int(m.group(2)))
            except ScheduleError as e:
                return str(e)
            return ("Quiet hours set: %02d:00-%02d:00 local. Reminders "
                    "landing inside them are deferred to the end, never "
                    "dropped." % (int(m.group(1)), int(m.group(2))))

        if re.match(r"^quiet hours off$", low):
            self.store.set_quiet(owner, None)
            return "Quiet hours off."

        if re.match(r"^export (my )?(reminders|schedule)$", low):
            import json
            return json.dumps(self.export(owner), indent=2)

        return None
