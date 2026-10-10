"""ports/models - pretrained-model provider abstraction (free-first, source-only).

This module owns the SEAM between Crayon's deterministic logic and optional
pretrained models (Hugging Face). It defines the three model capabilities the
find stack can use (embed, nli, rerank), the configuration surface, a circuit
breaker, and a guarded call path whose failure answer is ALWAYS the
deterministic fallback. It ships NO model weights, downloads nothing, and
never touches the network: real backends (local ONNX, hosted inference) are
integration-injected callables delivered through FunctionProvider, exactly
like find's injected fetcher/planner.

REUSE BOUNDARY (not a parallel stack): ports/find owns citation decisions and
its deterministic reference_support gate (support_fn injection point);
ports/find_personal owns personal-fact matching and ranking order; this
module owns none of those decisions. It provides only the guarded plumbing a
model signal must pass through. NLI/ranking output is an AID: adapters must
compose it so a model can help FIND support or ORDER results, never verify a
claim the deterministic evidence rules reject, and never grant permission.

Design invariants:

1. DETERMINISTIC DEFAULT: config defaults to provider "none" with every
   capability disabled. Merging or importing this module changes no behavior
   anywhere unless a caller explicitly enables a capability and injects a
   backend.
2. FALLBACK IS THE FAILURE ANSWER: guarded_call returns the caller-supplied
   fallback on disabled capability, unavailable capability, open breaker,
   timeout, or ANY backend exception. It never retries, never substitutes,
   never surfaces a partial provider result, and never raises for a provider
   failure. A timed-out backend thread may linger (daemon) but its late
   result is discarded and cannot affect state.
3. FAIL-CLOSED VALIDATION: unknown provider names, unknown capabilities,
   bad numeric bounds, non-callable backends and wrong argument types raise
   ModelsError (never raw TypeError/ValueError/KeyError) - before any backend
   is invoked where detectable up front, otherwise in place of the attempt.
   A ModelsError raised inside the injected call is a CALLER BUG: it
   propagates and is never counted against the breaker or answered with the
   fallback. CapabilityUnavailable and CallTimeout are backend failures and
   do take the fallback path. A raising fallback is re-raised as ModelsError.
4. NO GLOBAL MUTABLE STATE: configs are frozen; breakers and providers are
   caller-owned instances; snapshots are copies; reads never create or
   mutate state.
5. ONE ATTEMPT, NO RETRY: each guarded_call attempts the backend at most
   once and records exactly one breaker outcome.
6. MODEL OUTPUT IS NOT PROOF: this module attaches no semantic verdict to
   anything. Downstream gates keep their fail-closed evidence rules.
7. READ-ONLY, NOT MOUNTED: no network, no downloads, no action paths.
"""

import threading
import time
from dataclasses import dataclass, field

CAPABILITIES = ("embed", "nli", "rerank")

PROVIDERS = ("none", "local", "hosted")

DEFAULT_TIMEOUT_MS = 2000
DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_MS = 30000
DEFAULT_NLI_ENTAILMENT_MIN = 0.9

MAX_TIMEOUT_MS = 60000
MAX_CACHE_DIR_LEN = 512
MAX_TEXT_LEN = 100000
MAX_BATCH = 256


class ModelsError(Exception):
    """Module error: invalid input, config or wiring. Never a provider failure."""


class CapabilityUnavailable(ModelsError):
    """Raised by a provider that does not implement a requested capability."""


class CallTimeout(ModelsError):
    """Internal: the backend did not finish within timeout_ms."""


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _is_num(v):
    return (_is_int(v) or isinstance(v, float)) and not isinstance(v, bool)


def _check_str(value, name, allow_empty=False):
    if not isinstance(value, str) or (not allow_empty and not value):
        raise ModelsError("%s must be a non-empty string" % name)
    if len(value) > MAX_TEXT_LEN:
        raise ModelsError("%s exceeds MAX_TEXT_LEN" % name)
    return value


@dataclass(frozen=True)
class ProviderConfig:
    """Immutable provider configuration. Defaults are fully deterministic."""

    provider: str = "none"
    cache_dir: str | None = None
    timeout_ms: int = DEFAULT_TIMEOUT_MS
    failure_threshold: int = DEFAULT_FAILURE_THRESHOLD
    cooldown_ms: int = DEFAULT_COOLDOWN_MS
    enabled: frozenset = frozenset()
    nli_entailment_min: float = DEFAULT_NLI_ENTAILMENT_MIN

    def __post_init__(self):
        if self.provider not in PROVIDERS:
            raise ModelsError("unknown provider %r (want one of %s)"
                              % (self.provider, ", ".join(PROVIDERS)))
        if self.cache_dir is not None:
            if not isinstance(self.cache_dir, str) or not self.cache_dir:
                raise ModelsError("cache_dir must be a non-empty string or None")
            if len(self.cache_dir) > MAX_CACHE_DIR_LEN:
                raise ModelsError("cache_dir exceeds MAX_CACHE_DIR_LEN")
        if not _is_int(self.timeout_ms) or not (1 <= self.timeout_ms <= MAX_TIMEOUT_MS):
            raise ModelsError("timeout_ms must be an int in [1, %d]" % MAX_TIMEOUT_MS)
        if not _is_int(self.failure_threshold) or self.failure_threshold < 1:
            raise ModelsError("failure_threshold must be an int >= 1")
        if not _is_int(self.cooldown_ms) or self.cooldown_ms < 0:
            raise ModelsError("cooldown_ms must be an int >= 0")
        try:
            enabled = frozenset(self.enabled)
        except TypeError:
            raise ModelsError("enabled must be an iterable of capability names")
        for cap in enabled:
            if cap not in CAPABILITIES:
                raise ModelsError("unknown capability %r (want one of %s)"
                                  % (cap, ", ".join(CAPABILITIES)))
        object.__setattr__(self, "enabled", enabled)
        if not _is_num(self.nli_entailment_min) or not (0.0 <= float(self.nli_entailment_min) <= 1.0):
            raise ModelsError("nli_entailment_min must be a number in [0, 1]")
        object.__setattr__(self, "nli_entailment_min", float(self.nli_entailment_min))


_ENV_KEYS = {
    "PROVIDER": "provider",
    "CACHE_DIR": "cache_dir",
    "TIMEOUT_MS": "timeout_ms",
    "FAILURE_THRESHOLD": "failure_threshold",
    "COOLDOWN_MS": "cooldown_ms",
    "ENABLED": "enabled",
    "NLI_ENTAILMENT_MIN": "nli_entailment_min",
}


def from_env(mapping=None, prefix="CRAYON_MODEL_"):
    """Build a ProviderConfig from an env-like mapping (os.environ by default).

    Reads never create state. Unknown keys are ignored. Any malformed known
    value raises ModelsError naming the field; a missing key keeps the default.
    """
    if mapping is None:
        import os
        mapping = os.environ
    if not hasattr(mapping, "get"):
        raise ModelsError("mapping must be an env-like mapping")
    if not isinstance(prefix, str):
        raise ModelsError("prefix must be a string")
    kwargs = {}
    for key, target in _ENV_KEYS.items():
        raw = mapping.get(prefix + key)
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise ModelsError("%s%s must be a string" % (prefix, key))
        if target in ("timeout_ms", "failure_threshold", "cooldown_ms"):
            try:
                kwargs[target] = int(raw.strip())
            except ValueError:
                raise ModelsError("%s%s must be an integer" % (prefix, key))
        elif target == "nli_entailment_min":
            try:
                kwargs[target] = float(raw.strip())
            except ValueError:
                raise ModelsError("%s%s must be a number" % (prefix, key))
        elif target == "enabled":
            parts = [p.strip() for p in raw.split(",")]
            kwargs[target] = frozenset(p for p in parts if p)
        elif target == "cache_dir":
            kwargs[target] = raw if raw else None
        else:
            kwargs[target] = raw.strip()
    try:
        return ProviderConfig(**kwargs)
    except TypeError as exc:  # defensive; validation lives in __post_init__
        raise ModelsError("invalid config: %s" % exc)


class CircuitBreaker:
    """Consecutive-failure breaker with an injected clock.

    closed -> open after failure_threshold consecutive failures; any success
    resets the count. While open, allow() refuses until cooldown_ms elapse,
    then one half-open trial is permitted; its outcome closes or reopens.
    allow()/state/snapshot never mutate stored state.
    """

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"

    def __init__(self, failure_threshold=DEFAULT_FAILURE_THRESHOLD,
                 cooldown_ms=DEFAULT_COOLDOWN_MS, clock=None):
        if not _is_int(failure_threshold) or failure_threshold < 1:
            raise ModelsError("failure_threshold must be an int >= 1")
        if not _is_int(cooldown_ms) or cooldown_ms < 0:
            raise ModelsError("cooldown_ms must be an int >= 0")
        if clock is None:
            clock = time.monotonic
        if not callable(clock):
            raise ModelsError("clock must be callable")
        try:
            probe = clock()
        except Exception:
            raise ModelsError("clock() raised")
        if not _is_num(probe):
            raise ModelsError("clock() must return a number")
        self._threshold = failure_threshold
        self._cooldown_s = cooldown_ms / 1000.0
        self._clock = clock
        self._consecutive_failures = 0
        self._opened_at = None
        self._last_reason = None

    def _elapsed_since_open(self):
        if self._opened_at is None:
            return None
        now = self._clock()
        if not _is_num(now):
            raise ModelsError("clock() must return a number")
        return now - self._opened_at

    @property
    def state(self):
        if self._opened_at is None:
            return self.CLOSED
        elapsed = self._elapsed_since_open()
        if elapsed is not None and elapsed >= self._cooldown_s:
            return self.HALF_OPEN
        return self.OPEN

    def allow(self):
        """True when a call may be attempted now. Never mutates state."""
        return self.state != self.OPEN

    def record_success(self):
        self._consecutive_failures = 0
        self._opened_at = None
        self._last_reason = None

    def record_failure(self, reason=None):
        if reason is not None and not isinstance(reason, str):
            raise ModelsError("reason must be a string or None")
        was_half_open = self.state == self.HALF_OPEN
        self._consecutive_failures += 1
        self._last_reason = reason
        if self._consecutive_failures >= self._threshold:
            if self._opened_at is None or was_half_open:
                # closed->open transition, or a failed half-open trial:
                # (re)start the cooldown now so a down backend is not
                # hammered on every request.
                self._opened_at = self._clock()

    def snapshot(self):
        """A copy of breaker state; mutating it affects nothing."""
        return {
            "state": self.state,
            "consecutive_failures": self._consecutive_failures,
            "opened_at": self._opened_at,
            "last_reason": self._last_reason,
        }


class NoneProvider:
    """The default backend: implements nothing, fails closed."""

    def capabilities(self):
        return frozenset()

    def embed(self, texts):
        raise CapabilityUnavailable("provider 'none' has no embed backend")

    def nli(self, premise, hypothesis):
        raise CapabilityUnavailable("provider 'none' has no nli backend")

    def rerank(self, query, passages):
        raise CapabilityUnavailable("provider 'none' has no rerank backend")


def _validate_texts(texts):
    if not isinstance(texts, (list, tuple)):
        raise ModelsError("texts must be a list of strings")
    if len(texts) > MAX_BATCH:
        raise ModelsError("texts exceeds MAX_BATCH")
    out = []
    for i, t in enumerate(texts):
        out.append(_check_str(t, "texts[%d]" % i))
    return out


def _validate_passages(passages):
    if not isinstance(passages, (list, tuple)):
        raise ModelsError("passages must be a list of strings")
    if len(passages) > MAX_BATCH:
        raise ModelsError("passages exceeds MAX_BATCH")
    out = []
    for i, p in enumerate(passages):
        out.append(_check_str(p, "passages[%d]" % i))
    return out


class FunctionProvider:
    """Adapter over integration-injected callables (local ONNX, hosted API).

    The module never supplies its own callables and never performs I/O;
    whatever the integration injects is validated for callability here and
    for argument types on every call.
    """

    def __init__(self, embed_fn=None, nli_fn=None, rerank_fn=None):
        for name, fn in (("embed_fn", embed_fn), ("nli_fn", nli_fn),
                         ("rerank_fn", rerank_fn)):
            if fn is not None and not callable(fn):
                raise ModelsError("%s must be callable or None" % name)
        self._embed_fn = embed_fn
        self._nli_fn = nli_fn
        self._rerank_fn = rerank_fn

    def capabilities(self):
        caps = set()
        if self._embed_fn is not None:
            caps.add("embed")
        if self._nli_fn is not None:
            caps.add("nli")
        if self._rerank_fn is not None:
            caps.add("rerank")
        return frozenset(caps)

    def embed(self, texts):
        if self._embed_fn is None:
            raise CapabilityUnavailable("no embed backend injected")
        return self._embed_fn(_validate_texts(texts))

    def nli(self, premise, hypothesis):
        if self._nli_fn is None:
            raise CapabilityUnavailable("no nli backend injected")
        return self._nli_fn(_check_str(premise, "premise"),
                            _check_str(hypothesis, "hypothesis"))

    def rerank(self, query, passages):
        if self._rerank_fn is None:
            raise CapabilityUnavailable("no rerank backend injected")
        return self._rerank_fn(_check_str(query, "query"),
                               _validate_passages(passages))


def get_provider(config, backend=None):
    """Factory: returns a provider for config. Reads create no state.

    backend is the integration-supplied FunctionProvider-compatible object
    required when config.provider is "local" or "hosted"; "none" ignores it.
    """
    if not isinstance(config, ProviderConfig):
        raise ModelsError("config must be a ProviderConfig")
    if config.provider == "none":
        return NoneProvider()
    if backend is None:
        raise ModelsError("provider %r requires an injected backend" % config.provider)
    for meth in ("capabilities", "embed", "nli", "rerank"):
        if not callable(getattr(backend, meth, None)):
            raise ModelsError("backend must provide %s()" % meth)
    return backend


def _run_with_timeout(fn, timeout_ms):
    """Run fn on a daemon thread; raise CallTimeout if it outlives the budget.

    A timed-out thread may linger; its late result is discarded by the caller
    and can never be observed. join() provides the happens-before edge for
    the success/error box.
    """
    box = {}

    def target():
        try:
            box["value"] = fn()
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller thread
            box["error"] = exc

    worker = threading.Thread(target=target, daemon=True)
    worker.start()
    worker.join(timeout_ms / 1000.0)
    if worker.is_alive():
        raise CallTimeout("backend exceeded timeout_ms=%d" % timeout_ms)
    if "error" in box:
        raise box["error"]
    return box["value"]


def guarded_call(config, breaker, provider, capability, call, fallback):
    """Attempt one model call; on ANY failure return fallback().

    call is a zero-arg callable invoking the provider (e.g.
    lambda: provider.nli(premise, hypothesis)); fallback is a zero-arg
    callable producing the deterministic answer. Exactly one backend attempt
    and one breaker outcome per invocation. Provider failures never raise;
    invalid arguments/config raise ModelsError before any attempt.
    """
    if not isinstance(config, ProviderConfig):
        raise ModelsError("config must be a ProviderConfig")
    if not isinstance(breaker, CircuitBreaker):
        raise ModelsError("breaker must be a CircuitBreaker")
    if capability not in CAPABILITIES:
        raise ModelsError("unknown capability %r" % (capability,))
    if not callable(call):
        raise ModelsError("call must be callable")
    if not callable(fallback):
        raise ModelsError("fallback must be callable")
    caps_fn = getattr(provider, "capabilities", None)
    if not callable(caps_fn):
        raise ModelsError("provider must provide capabilities()")
    try:
        caps = frozenset(caps_fn())
    except ModelsError:
        raise
    except Exception:
        raise ModelsError("provider capabilities() failed or returned non-iterable")

    def answer():
        try:
            return fallback()
        except ModelsError:
            raise
        except Exception as exc:
            raise ModelsError("fallback raised: %s" % exc)

    if capability not in config.enabled:
        return answer()
    if capability not in caps:
        return answer()
    if not breaker.allow():
        return answer()
    try:
        value = _run_with_timeout(call, config.timeout_ms)
    except (CapabilityUnavailable, CallTimeout) as exc:
        # genuine backend failures: count them, answer with the fallback
        breaker.record_failure(reason=type(exc).__name__)
        return answer()
    except ModelsError:
        # the module's own validation (bad arguments inside call) is a
        # programming error, not a backend failure: raise, never swallow,
        # and do not count it against the breaker.
        raise
    except Exception as exc:
        breaker.record_failure(reason=type(exc).__name__)
        return answer()
    breaker.record_success()
    return value
