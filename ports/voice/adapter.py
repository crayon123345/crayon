"""Synthetic voice ADAPTER client for the canonical voice API v1 (REV3).

Staging only: transport is injected, there are no HTTP routes, no provider
calls, no device attestation, no activation. Implements the adapter-side
obligations of the accepted contract:

- No provider work before a successful redeem (fail closed).
- ACTIVE revalidation: a drive loop revalidates immediately before provider
  connect and every revalidate_seconds (15) of WALL-CLOCK time while active,
  including while turn work is blocking; ANY denial or an
  unavailable/timed-out/failed validation closes the lease. No grace
  fallback. Ordinary transport failures (builtin TimeoutError/OSError) fail
  closed exactly like an explicit TransportTimeout.
- Caps enforced INCREMENTALLY: elapsed time from redeem and audio bytes are
  tracked per lease; the first cap breach ends the turn (lease closed).
  Negative byte increments are rejected; server-provided caps are CLAMPED
  to the contract bounds (600s / 20MB) - a server can only tighten them.
- Redeem ticket travels in the request BODY only (never in a URL).
- Local binding validation: observed_origin must BE a canonical https
  origin (literal lowercase scheme and host, no credentials, path, query,
  fragment, whitespace, empty labels, trailing dot, or out-of-range port;
  anything needing normalization is rejected outright) AND be on the
  explicit allowlist; canonical redeem responses must echo the bound
  mode/origin and carry the required fields; revalidate responses must
  echo the lease's lease_id, request_id and expires_at; active:false is a
  close.
- NaN/Infinity in ANY timing or byte value taken from a canonical
  response (audio caps, revalidate cadence, expires_at) is REJECTED as
  non-conformant, never clamped or defaulted - non-finite comparisons are
  always false and would silently disable caps or the liveness cadence.
- seq is monotonic per request. Integer inputs (audio byte increments)
  are strictly parsed: bools, fractional/non-finite floats, and strings
  with signs, whitespace or float syntax are rejected, never truncated.
- Terminal guard: after a final, only an IDENTICAL final retry while the
  lease is still live is valid (canonical duplicate path); any other
  progress/final is rejected locally.
- Ruling (b): a /final attempt whose transport response is lost (timeout
  or ordinary transport failure) or that is denied after lease/session
  expiry or revocation (401/403/410) means commit status UNKNOWN: the
  adapter closes, reports unknown, and never replays provider work or
  effects. Resolution is only via the authorized browser read-only
  /voice/result within the original 300s TTL.
"""
import math
import re
import threading
import time

PROGRESS_TYPES = {"accepted", "progress", "transcript_partial", "transcript_final",
                  "answer_partial", "answer_final", "review_required", "artifact",
                  "blocked", "done"}
FINAL_STATES = {"done", "blocked"}
REVALIDATE_SECONDS = 15
RECOVERY_TTL_SECONDS = 300
REQUIRED_REDEEM_FIELDS = ("lease_id", "request_id", "conversation_id",
                          "mode", "origin", "expires_at", "audio_limits")
# Contract bounds: server-provided caps may only tighten these, never widen.
CONTRACT_MAX_SECONDS = 600
CONTRACT_MAX_BYTES = 20000000

# Canonical origin form: literal lowercase https, lowercase host labels
# (no empty labels, no leading/trailing dot, no trailing hyphen), optional
# numeric port. Anything needing normalization to compare equal is not an
# exact origin.
_ORIGIN_RE = re.compile(
    r"^https://([a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
    r"(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*)(?::([0-9]{1,5}))?$")


def _require_finite(value, field):
    """A timing/byte value taken from a canonical response must be a real
    finite number. NaN/Infinity (and bools/non-numbers) are non-conformant
    and REJECTED: non-finite comparisons are always false, which would
    silently disable a cap or the liveness cadence."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AdapterError(f"bad_binding:{field}", 502)
    v = float(value)
    if not math.isfinite(v):
        raise AdapterError(f"bad_binding:{field}", 502)
    return v


def _strict_int(value):
    """Strict integer parsing: real ints, integral floats, and digit-only
    strings. Bools, fractional/non-finite floats, and strings with signs,
    whitespace or float syntax are rejected (None), never truncated."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if math.isfinite(value) and value.is_integer() else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


class TransportTimeout(Exception):
    """The canonical did not answer; the request may or may not have landed."""


# Any of these mean "no usable response": fail closed everywhere.
TRANSPORT_FAILURES = (TransportTimeout, TimeoutError, OSError)


class AdapterError(Exception):
    def __init__(self, code, status):
        super().__init__(f"{status}: {code}")
        self.code = code
        self.status = status


class CapExceeded(AdapterError):
    def __init__(self, which):
        super().__init__(f"cap_exceeded:{which}", 413 if which == "bytes" else 410)
        self.which = which


class CommitUnknown(Exception):
    """Ruling (b): final may or may not be committed. Never replay."""


class Lease:
    def __init__(self, body, clock):
        self.lease_id = body["lease_id"]
        self.request_id = body["request_id"]
        self.conversation_id = body["conversation_id"]
        self.mode = body["mode"]
        self.origin = body["origin"]
        self.expires_at = _require_finite(body["expires_at"], "expires_at")
        lim = body.get("audio_limits")
        if not isinstance(lim, dict):
            raise AdapterError("bad_binding:audio_limits", 502)
        # Clamp server-provided caps to the contract bounds: the server may
        # tighten but never widen beyond 600s / 20MB. Non-finite values are
        # rejected, not clamped (see _require_finite).
        self.audio_limits = {
            "max_seconds": (min(_require_finite(lim["max_seconds"],
                                                "audio_limits.max_seconds"),
                                CONTRACT_MAX_SECONDS)
                            if "max_seconds" in lim else CONTRACT_MAX_SECONDS),
            "max_bytes": (min(_require_finite(lim["max_bytes"],
                                              "audio_limits.max_bytes"),
                              CONTRACT_MAX_BYTES)
                          if "max_bytes" in lim else CONTRACT_MAX_BYTES),
        }
        # Clamp incoming timing to the contract cadence: the server may
        # tighten but NEVER widen the 15s revalidation cadence (a 3600s
        # server value would silently disable active liveness AND stretch
        # the cancellation join timeout). Non-positive/garbage -> contract;
        # NaN/Infinity -> rejected (comparisons would be silently false).
        try:
            rs = float(body.get("revalidate_seconds", REVALIDATE_SECONDS))
        except (TypeError, ValueError):
            rs = REVALIDATE_SECONDS
        if not math.isfinite(rs):
            raise AdapterError("bad_binding:revalidate_seconds", 502)
        self.revalidate_seconds = (REVALIDATE_SECONDS if rs <= 0
                                   else min(rs, REVALIDATE_SECONDS))
        self.redeemed_at = clock()
        self.bytes_used = 0
        self.active = True
        self.closed_reason = None
        self.seq = 0
        self.final_payload = None


class VoiceAdapter:
    """One adapter instance per deployment; one Lease per redeemed ticket."""

    def __init__(self, transport, credential, clock=time.time,
                 allowed_origins=None):
        """transport(method, path, headers, body) -> (status, dict);
        may raise TransportTimeout/TimeoutError/OSError when no response
        arrives. allowed_origins: REQUIRED non-empty iterable of the exact
        https origins redeem may bind to - the confirmed merged-deployment
        configuration. There is NO default: an absent or empty allowlist
        fails closed at construction, because a shipped default cannot
        establish origin authority."""
        if not allowed_origins:
            raise ValueError(
                "allowed_origins is required: explicit merged-deployment "
                "configuration, fail closed when absent")
        self._t = transport
        self._cred = credential
        self._clock = clock
        self._allowed = frozenset(allowed_origins)
        self._lock = threading.Lock()
        self._unclean = False  # set when a worker survives cancellation

    # -- transport ------------------------------------------------------
    def _call(self, path, body):
        status, data = self._t("POST", path,
                               {"Authorization": f"Bearer {self._cred}"}, body)
        if status >= 400:
            raise AdapterError((data or {}).get("error_code", "unknown"), status)
        return data or {}

    # -- redeem ----------------------------------------------------------
    def _check_origin(self, observed_origin):
        """Exact-origin strictness: the origin must BE its canonical form
        (see _ORIGIN_RE) - no case folding, whitespace, control characters,
        empty labels, trailing dot, credentials, path, query, fragment, or
        out-of-range port - and the exact string must be on the explicit
        allowlist. Anything that would need normalization to compare equal
        is rejected outright instead of being normalized into a match."""
        if not isinstance(observed_origin, str):
            raise AdapterError("bad_origin", 400)
        m = _ORIGIN_RE.match(observed_origin)
        if not m:
            raise AdapterError("bad_origin", 400)
        port = m.group(2)
        if port is not None and not 1 <= int(port) <= 65535:
            raise AdapterError("bad_origin", 400)
        if observed_origin not in self._allowed:
            raise AdapterError("bad_origin:not_allowlisted", 400)

    def redeem(self, ticket, observed_origin, mode):
        """Redeem a ticket taken from the FIRST websocket frame only.
        The ticket must never appear in a URL; it goes in the body."""
        self._require_clean()
        if not ticket or not mode:
            raise AdapterError("malformed", 400)
        self._check_origin(observed_origin)
        data = self._call("/voice/internal/redeem",
                          {"version": 1, "ticket": ticket,
                           "observed_origin": observed_origin, "mode": mode})
        missing = [f for f in REQUIRED_REDEEM_FIELDS if f not in data]
        if missing:
            raise AdapterError("bad_binding:missing:" + ",".join(missing), 502)
        if data["mode"] != mode or data["origin"] != observed_origin:
            raise AdapterError("bad_binding:echo", 502)
        return Lease(data, self._clock)

    # -- caps ------------------------------------------------------------
    def _check_caps(self, lease):
        lim = lease.audio_limits
        if self._clock() - lease.redeemed_at >= lim["max_seconds"]:
            self._close(lease, "cap:seconds")
            raise CapExceeded("seconds")
        if lease.bytes_used >= lim["max_bytes"]:
            self._close(lease, "cap:bytes")
            raise CapExceeded("bytes")

    def note_audio_bytes(self, lease, n):
        """Incrementally account audio bytes; first cap breach ends the turn.
        Negative or non-numeric increments are rejected, never applied."""
        self._require_clean()
        with self._lock:
            self._require_active(lease)
            n = _strict_int(n)
            if n is None or n < 0:
                raise AdapterError("bad_increment", 400)
            lease.bytes_used += n
            self._check_caps(lease)

    # -- revalidate --------------------------------------------------------
    def revalidate(self, lease):
        """Check before provider connect and every 15s wall-clock. ANY
        denial, non-active response, binding mismatch, or
        unavailable/timed-out/failed validation closes the lease."""
        self._require_clean()
        try:
            data = self._call("/voice/internal/revalidate",
                              {"version": 1, "lease_id": lease.lease_id})
        except TRANSPORT_FAILURES as e:
            self._close(lease, "validation_unavailable")
            raise AdapterError("validation_unavailable", 503) from e
        except AdapterError:
            self._close(lease, "denied")
            raise
        # Full response binding: the canonical must answer about THIS
        # lease. lease_id is REQUIRED in the response - an omitted lease_id
        # is non-conformant (coordinated canonical+adapter spec), not a
        # reason to relax the check.
        if (data.get("lease_id") != lease.lease_id
                or data.get("request_id") != lease.request_id
                or data.get("expires_at") != lease.expires_at):
            self._close(lease, "bad_binding")
            raise AdapterError("bad_binding:revalidate", 502)
        if data.get("active") is not True:
            self._close(lease, "inactive")
            raise AdapterError("inactive", 410)
        return True

    def drive_once(self, lease):
        """One active-cadence step: caps check + revalidate. Any exception
        here means the lease closed."""
        self._require_clean()
        with self._lock:
            self._require_active(lease)
            self._check_caps(lease)
        return self.revalidate(lease)

    def run_turn(self, lease, work, sleep=time.sleep):
        """Active cadence loop with WALL-CLOCK cadence and cooperative
        cancellation. Order of operations is load-bearing:

        1. The FIRST validation (drive_once: caps + revalidate) completes
           BEFORE any worker/provider work begins - a denied startup means
           the worker is never started, so no effect can exist.
        2. work(lease, cancel) then runs ONCE on a worker thread and may
           block; the calling thread keeps revalidating every
           lease.revalidate_seconds of wall-clock time regardless of how
           long work blocks.
        3. On ANY failed check the lease closes, `cancel` (a
           threading.Event) is set, and the worker is JOINED (bounded by
           the clamped cadence) before the error propagates.

        SCAFFOLD LIMITATION (explicit): cancellation is COOPERATIVE ONLY.
        Threads cannot be force-killed: a worker that ignores `cancel`
        and outlives the bounded join is DETECTED (is_alive) and surfaced
        as worker_survived_cancel, the adapter marks itself permanently
        UNCLEAN and refuses all further work - but post-close effects
        from the surviving worker are UNBOUNDED. There is NO
        no-after-close-effects guarantee for noncooperative work.
        Enforceable cancellation (process isolation) is future work.

        Returns work's result; re-raises work errors."""
        # (a) validate first: a denial here means the worker never runs.
        self.drive_once(lease)
        cancel = threading.Event()
        outcome = {}

        def runner():
            try:
                outcome["value"] = work(lease, cancel)
            except BaseException as e:  # propagate after the loop exits
                outcome["error"] = e
            finally:
                outcome["finished"] = True

        t = threading.Thread(target=runner, daemon=True)
        t.start()
        next_check = self._clock() + lease.revalidate_seconds
        try:
            while True:
                now = self._clock()
                if now >= next_check:
                    self.drive_once(lease)  # raises -> close -> cancel+join
                    next_check = now + lease.revalidate_seconds
                if outcome.get("finished"):
                    break
                sleep(min(max(next_check - self._clock(), 0.0), 0.05) or 0.001)
        except BaseException as e:
            cancel.set()                    # (b) cooperative cancellation
            t.join(timeout=lease.revalidate_seconds)  # joined before raise
            if t.is_alive():
                # Non-cooperative worker survived the bounded join. This is
                # DETECTED and surfaced, never silently ignored: the lease
                # is closed, but the worker may still produce effects.
                # SCAFFOLD LIMITATION: threads cannot be force-killed, so
                # there is NO post-close guarantee for noncooperative work.
                # The adapter marks itself permanently unclean and refuses
                # all further work. Original denial chains as __cause__.
                self._unclean = True
                raise AdapterError(
                    "worker_survived_cancel: cancellation is cooperative "
                    "only; worker ignored cancel and outlived the join "
                    "timeout - adapter is now UNCLEAN and refuses further "
                    "work; post-close effects from the survivor are "
                    "UNBOUNDED (enforceable process isolation is future "
                    "work)", 500) from e
            raise
        cancel.set()
        t.join(timeout=lease.revalidate_seconds)      # cleanup join
        if t.is_alive():
            self._unclean = True
            raise AdapterError(
                "worker_survived_cancel: worker outlived the cleanup join "
                "timeout after a finished turn - adapter is now UNCLEAN "
                "and refuses further work", 500)
        if "error" in outcome:
            raise outcome["error"]
        return outcome.get("value", False)

    # -- progress/final ----------------------------------------------------
    def progress(self, lease, type_, state, text=None):
        self._require_clean()
        with self._lock:
            self._require_active(lease)
            if lease.final_payload is not None:
                raise AdapterError("terminal", 409)
            if type_ not in PROGRESS_TYPES:
                raise AdapterError("bad_type", 400)
            self._check_caps(lease)
            lease.seq += 1
            body = {"version": 1, "lease_id": lease.lease_id, "seq": lease.seq,
                    "type": type_, "state": state}
            if text is not None:
                body["text"] = text
            try:
                return self._call("/voice/internal/progress", body)
            except TRANSPORT_FAILURES as e:
                self._close(lease, "transport_failure")
                raise AdapterError("transport_failure", 503) from e
            except AdapterError:
                self._close(lease, "denied")
                raise

    def final(self, lease, state, transcript_text=None, answer_text=None,
              error_code=None):
        """One terminal commit. Identical retry while live: allowed
        (canonical duplicate path). Lost response (timeout OR ordinary
        transport failure) or liveness denial: CommitUnknown + close,
        never replay."""
        self._require_clean()
        with self._lock:
            self._require_active(lease)
            if state not in FINAL_STATES:
                raise AdapterError("bad_state", 400)
            self._check_caps(lease)
            body = {"version": 1, "lease_id": lease.lease_id,
                    "request_id": lease.request_id, "seq": lease.seq + 1,
                    "type": "done", "state": state}
            if transcript_text is not None:
                body["transcript_text"] = transcript_text
            if answer_text is not None:
                body["answer_text"] = answer_text
            if error_code is not None:
                body["error_code"] = error_code
            if lease.final_payload is not None:
                prior = dict(lease.final_payload)
                new_core, prior_core = dict(body), prior
                new_core.pop("seq"); prior_core.pop("seq")
                if new_core != prior_core:
                    raise AdapterError("final_conflict", 409)
                body = lease.final_payload  # exact resend for the duplicate path
            else:
                lease.seq += 1
                lease.final_payload = body
            try:
                return self._call("/voice/internal/final", body)
            except TRANSPORT_FAILURES as e:
                # The commit may have landed; status is UNKNOWN. No replay.
                self._close(lease, "commit_unknown")
                raise CommitUnknown(
                    "final response lost; commit status unknown; recover via "
                    "authorized browser read-only /voice/result within "
                    f"{RECOVERY_TTL_SECONDS}s of the original final; "
                    "never replay provider work or effects") from e
            except AdapterError as e:
                self._close(lease, "denied")
                if e.status in (401, 403, 410):
                    raise CommitUnknown(
                        "final denied after expiry/revocation; commit status "
                        "unknown; recover via authorized browser read-only "
                        f"/voice/result within {RECOVERY_TTL_SECONDS}s; "
                        "never replay provider work or effects") from e
                raise

    # -- helpers ---------------------------------------------------------
    def _require_clean(self):
        """After an unclean close (a worker survived cancellation) the
        adapter permanently refuses further work: its post-close state can
        no longer be guaranteed."""
        if self._unclean:
            raise AdapterError(
                "adapter_unclean: a worker survived cancellation earlier; "
                "this adapter refuses further work (scaffold fail-safe)",
                500)

    def provider_connect_guard(self, lease):
        """Must pass immediately before any provider connect."""
        self._require_clean()
        return self.revalidate(lease)

    @staticmethod
    def _close(lease, reason):
        lease.active = False
        lease.closed_reason = reason

    @staticmethod
    def _require_active(lease):
        if not lease.active:
            raise AdapterError("lease_closed", 410)
