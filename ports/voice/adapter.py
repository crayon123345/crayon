"""Synthetic voice ADAPTER client for the canonical voice API v1 (REV3).

Staging only: transport is injected, there are no HTTP routes, no provider
calls, no device attestation, no activation. Implements the adapter-side
obligations of the accepted contract:

- No provider work before a successful redeem (fail closed).
- ACTIVE revalidation: a drive loop revalidates immediately before provider
  connect and every revalidate_seconds (15) while active; ANY denial or an
  unavailable/timed-out validation closes the lease. No grace fallback.
- Caps enforced INCREMENTALLY: elapsed time from redeem and audio bytes are
  tracked per lease; the first cap breach ends the turn (lease closed).
- Redeem ticket travels in the request BODY only (never in a URL).
- Local binding validation: observed_origin must be a non-null https://
  origin; canonical redeem/revalidate responses must echo the bound
  mode/origin and carry the required fields, and active:false is a close.
- seq is monotonic per request.
- Terminal guard: after a final, only an IDENTICAL final retry while the
  lease is still live is valid (canonical duplicate path); any other
  progress/final is rejected locally.
- Ruling (b): a /final attempt whose transport response is lost (timeout)
  or that is denied after lease/session expiry or revocation (401/403/410)
  means commit status UNKNOWN: the adapter closes, reports unknown, and
  never replays provider work or effects. Resolution is only via the
  authorized browser read-only /voice/result within the original 300s TTL.
"""
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


class TransportTimeout(Exception):
    """The canonical did not answer; the request may or may not have landed."""


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
        self.expires_at = body["expires_at"]
        self.audio_limits = body["audio_limits"]
        self.revalidate_seconds = body.get("revalidate_seconds", REVALIDATE_SECONDS)
        self.redeemed_at = clock()
        self.bytes_used = 0
        self.active = True
        self.closed_reason = None
        self.seq = 0
        self.final_payload = None


class VoiceAdapter:
    """One adapter instance per deployment; one Lease per redeemed ticket."""

    def __init__(self, transport, credential, clock=time.time):
        """transport(method, path, headers, body) -> (status, dict);
        may raise TransportTimeout when no response arrives."""
        self._t = transport
        self._cred = credential
        self._clock = clock
        self._lock = threading.Lock()

    # -- transport ------------------------------------------------------
    def _call(self, path, body):
        status, data = self._t("POST", path,
                               {"Authorization": f"Bearer {self._cred}"}, body)
        if status >= 400:
            raise AdapterError((data or {}).get("error_code", "unknown"), status)
        return data or {}

    # -- redeem ----------------------------------------------------------
    @staticmethod
    def _check_origin(observed_origin):
        if (not isinstance(observed_origin, str)
                or not observed_origin.startswith("https://")):
            raise AdapterError("bad_origin", 400)

    def redeem(self, ticket, observed_origin, mode):
        """Redeem a ticket taken from the FIRST websocket frame only.
        The ticket must never appear in a URL; it goes in the body."""
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
        lim = lease.audio_limits or {}
        max_seconds = lim.get("max_seconds", 600)
        max_bytes = lim.get("max_bytes", 20000000)
        if self._clock() - lease.redeemed_at >= max_seconds:
            self._close(lease, "cap:seconds")
            raise CapExceeded("seconds")
        if lease.bytes_used >= max_bytes:
            self._close(lease, "cap:bytes")
            raise CapExceeded("bytes")

    def note_audio_bytes(self, lease, n):
        """Incrementally account audio bytes; first cap breach ends the turn."""
        with self._lock:
            self._require_active(lease)
            lease.bytes_used += int(n)
            self._check_caps(lease)

    # -- revalidate --------------------------------------------------------
    def revalidate(self, lease):
        """Check before provider connect and every 15s. ANY denial,
        non-active response, or unavailable/timed-out validation closes."""
        try:
            data = self._call("/voice/internal/revalidate",
                              {"version": 1, "lease_id": lease.lease_id})
        except TransportTimeout as e:
            self._close(lease, "validation_unavailable")
            raise AdapterError("validation_unavailable", 503) from e
        except AdapterError:
            self._close(lease, "denied")
            raise
        if data.get("active") is not True:
            self._close(lease, "inactive")
            raise AdapterError("inactive", 410)
        return True

    def drive_once(self, lease):
        """One active-cadence step: caps check + revalidate. The embedding
        runtime calls this every lease.revalidate_seconds while the turn is
        active (see run_turn); any exception here means the lease closed."""
        with self._lock:
            self._require_active(lease)
            self._check_caps(lease)
        return self.revalidate(lease)

    def run_turn(self, lease, work, sleep=time.sleep):
        """Active cadence loop: runs `work(lease)` while revalidating every
        revalidate_seconds; closes on any denial/timeout/cap breach.
        `work` is called between checks and must be interruptible by the
        caller (synthetic staging: typically a step function returning True
        when finished)."""
        while lease.active:
            self.drive_once(lease)
            if work(lease):
                return True
            sleep(lease.revalidate_seconds)
        return False

    # -- progress/final ----------------------------------------------------
    def progress(self, lease, type_, state, text=None):
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
            except AdapterError:
                self._close(lease, "denied")
                raise

    def final(self, lease, state, transcript_text=None, answer_text=None,
              error_code=None):
        """One terminal commit. Identical retry while live: allowed
        (canonical duplicate path). Lost response or liveness denial:
        CommitUnknown + close, never replay."""
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
            except TransportTimeout as e:
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
    def provider_connect_guard(self, lease):
        """Must pass immediately before any provider connect."""
        return self.revalidate(lease)

    @staticmethod
    def _close(lease, reason):
        lease.active = False
        lease.closed_reason = reason

    @staticmethod
    def _require_active(lease):
        if not lease.active:
            raise AdapterError("lease_closed", 410)
