"""Synthetic voice ADAPTER client for the canonical voice API v1 (REV3).

Staging only: transport is injected, there are no HTTP routes, no provider
calls, no device attestation, no activation. Implements the adapter-side
obligations of the accepted contract:

- No provider work before a successful redeem (fail closed).
- Revalidate immediately before provider connect and every 15s while active;
  close on 410 or unavailable validation. No grace fallback.
- Redeem ticket travels in the request BODY only (never in a URL).
- seq is monotonic per request.
- transcript_final/answer_final are component progress events; the terminal
  commit is exactly one /final envelope (type done, state done|blocked).
- Ruling (b): a /final retry denied after lease/session expiry or revocation
  (401/403/410) means commit status UNKNOWN, never failed. The adapter stops,
  reports unknown, and never replays provider work or effects. Resolution is
  only via the authorized browser read-only /voice/result within the original
  300s TTL; if unavailable the status stays unknown.
"""
import threading

PROGRESS_TYPES = {"accepted", "progress", "transcript_partial", "transcript_final",
                  "answer_partial", "answer_final", "review_required", "artifact",
                  "blocked", "done"}
FINAL_STATES = {"done", "blocked"}
REVALIDATE_SECONDS = 15
RECOVERY_TTL_SECONDS = 300


class AdapterError(Exception):
    def __init__(self, code, status):
        super().__init__(f"{status}: {code}")
        self.code = code
        self.status = status


class CommitUnknown(Exception):
    """Ruling (b): final may or may not be committed. Never replay."""


class Lease:
    def __init__(self, body):
        self.lease_id = body["lease_id"]
        self.request_id = body["request_id"]
        self.conversation_id = body["conversation_id"]
        self.mode = body["mode"]
        self.origin = body["origin"]
        self.expires_at = body["expires_at"]
        self.audio_limits = body["audio_limits"]
        self.active = True
        self.seq = 0
        self.final_sent = False


class VoiceAdapter:
    """One adapter instance per deployment; one Lease per redeemed ticket."""

    def __init__(self, transport, credential):
        """transport(method, path, headers, body) -> (status, dict)."""
        self._t = transport
        self._cred = credential
        self._lock = threading.Lock()

    def _call(self, path, body):
        status, data = self._t("POST", path,
                               {"Authorization": f"Bearer {self._cred}"}, body)
        if status >= 400:
            raise AdapterError(data.get("error_code", "unknown"), status)
        return data

    def redeem(self, ticket, observed_origin, mode):
        """Redeem a ticket taken from the FIRST websocket frame only.
        The ticket must never appear in a URL; it goes in the body."""
        if not ticket or not observed_origin or not mode:
            raise AdapterError("malformed", 400)
        data = self._call("/voice/internal/redeem",
                          {"version": 1, "ticket": ticket,
                           "observed_origin": observed_origin, "mode": mode})
        return Lease(data)

    def revalidate(self, lease):
        """Check before provider connect and every 15s. Any denial closes."""
        try:
            self._call("/voice/internal/revalidate",
                       {"version": 1, "lease_id": lease.lease_id})
        except AdapterError:
            lease.active = False
            raise
        return True

    def progress(self, lease, type_, state, text=None):
        with self._lock:
            self._require_active(lease)
            if type_ not in PROGRESS_TYPES:
                raise AdapterError("bad_type", 400)
            lease.seq += 1
            body = {"version": 1, "lease_id": lease.lease_id, "seq": lease.seq,
                    "type": type_, "state": state}
            if text is not None:
                body["text"] = text
            try:
                return self._call("/voice/internal/progress", body)
            except AdapterError:
                lease.active = False
                raise

    def final(self, lease, state, transcript_text=None, answer_text=None,
              error_code=None):
        """One terminal commit. On auth/liveness denial after send: the commit
        status is UNKNOWN (ruling b) - stop, do not replay, recover read-only."""
        with self._lock:
            self._require_active(lease)
            if state not in FINAL_STATES:
                raise AdapterError("bad_state", 400)
            lease.seq += 1
            body = {"version": 1, "lease_id": lease.lease_id,
                    "request_id": lease.request_id, "seq": lease.seq,
                    "type": "done", "state": state}
            if transcript_text is not None:
                body["transcript_text"] = transcript_text
            if answer_text is not None:
                body["answer_text"] = answer_text
            if error_code is not None:
                body["error_code"] = error_code
            lease.final_sent = True
            try:
                return self._call("/voice/internal/final", body)
            except AdapterError as e:
                lease.active = False
                if e.status in (401, 403, 410):
                    # 410/403/401 here does NOT prove the commit failed.
                    raise CommitUnknown(
                        "final commit status unknown; recover via authorized "
                        "browser read-only /voice/result within "
                        f"{RECOVERY_TTL_SECONDS}s of the original final; "
                        "never replay provider work or effects") from e
                raise

    def provider_connect_guard(self, lease):
        """Must pass immediately before any provider connect."""
        return self.revalidate(lease)

    @staticmethod
    def _require_active(lease):
        if not lease.active:
            raise AdapterError("lease_closed", 410)
