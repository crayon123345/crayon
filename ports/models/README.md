# /ports/models - pretrained-model provider abstraction (free-first, source-only)

The seam between Crayon's deterministic logic and optional pretrained
Hugging Face models. Defines the three model capabilities the find stack
can use (embed, nli, rerank), the configuration surface, a circuit
breaker, and a guarded call path whose failure answer is ALWAYS the
deterministic fallback. Ships NO model weights, downloads nothing, and
never touches the network: real backends (local ONNX, hosted inference)
are integration-injected callables delivered through FunctionProvider,
exactly like find's injected fetcher/planner.

## REUSE BOUNDARY (not a parallel stack)

ports/find owns citation decisions and its deterministic
reference_support gate (the support_fn injection point);
ports/find_personal owns personal-fact matching and ranking order. This
module owns NONE of those decisions - only the guarded plumbing a model
signal must pass through. NLI/ranking output is an AID: adapters must
compose it so a model can help FIND support or ORDER results, never
verify a claim the deterministic evidence rules reject, and never grant
permission. Model output is not proof.

## STATUS: STAGING MODULE - NOT MOUNTED, NOT LIVE

Complete as a tested module (61/61 tests, 10/10 repeat runs stable) but
NOT wired into the running app. Named limits, exactly:

1. DETERMINISTIC DEFAULT: ProviderConfig defaults to provider "none"
   with every capability disabled; importing or merging this module
   changes no behavior anywhere until a caller explicitly enables a
   capability AND injects a backend.
2. FALLBACK IS THE FAILURE ANSWER: guarded_call returns the
   caller-supplied fallback on disabled capability, unavailable
   capability, open breaker, timeout, or ANY backend exception. No
   retries, no substitution, no partial results, never raises for a
   provider failure. Exactly one backend attempt and one breaker
   outcome per call (probe: slow backend returns fallback in <timeout
   while its late result is discarded; call-count probes show one
   attempt, zero retries).
3. CALLER BUGS ARE NOT BACKEND FAILURES: a ModelsError raised inside
   the injected call (bad arguments) propagates and is NOT counted
   against the breaker; CapabilityUnavailable/CallTimeout take the
   fallback path; a raising fallback is re-raised as ModelsError.
   Backend-raised SystemExit/KeyboardInterrupt are boxed on the worker
   thread (BackendRaised) and take the failure path under the original
   exception's name; an interrupt on the CALLER thread still propagates.
   Backends must not raise ModelsError - every validating adapter
   converts it to CapabilityUnavailable (ports/find_support_nli and
   ports/find_personal_semantic both do, each regression-tested with
   a real backend-raise probe).
4. BREAKER: opens after failure_threshold consecutive failures; any
   success closes and resets. Half-open after cooldown_ms; a FAILED
   half-open trial re-opens with a FRESH cooldown so a down backend is
   not hammered per request. allow() does not mutate state, so the
   breaker is NOT thread-safe - own one per single-threaded caller
   (guarded_call records each outcome synchronously, making trials
   sequential under that use). snapshot() returns a copy. A
   decreasing/stuck clock fails closed (breaker stays open).
   INTEGRATION LIMIT (round-9, review 5480642386): while half-open,
   allow() admits trials until an outcome is recorded - guarded_call
   records synchronously, but an integration calling the breaker
   directly must bound concurrent half-open trials itself.
5. TIMEOUT CAVEAT: a timed-out backend runs on a daemon thread that may
   linger; its late result is discarded and can never be observed or
   affect state. Hard cancellation of a stuck backend is production
   wiring's job, not this module's.
6. VALIDATION IS FAIL-CLOSED: unknown provider names/capabilities, bad
   numeric bounds, non-callable backends, enabled given as a plain
   string, and wrong argument types raise ModelsError - never raw
   TypeError/ValueError/KeyError.
7. VALIDATION LIVES IN THE ADAPTERS, NOT THE PROVIDER (round-9,
   review 5480642386): FunctionProvider is a generic callable carrier - it
   performs NO output validation and NO thresholding. Integration
   wiring must route every backend through a validating adapter
   (ports/find_support_nli for citation support, ports/
   find_personal_semantic for embed/rerank); wiring a raw backend
   callable straight to the app skips every safeguard and is refused
   by design.
8. NO WEIGHTS, NO DOWNLOADS, NO NETWORK: local/hosted backends arrive
   as injected callables in later adapter PRs. Model license/terms
   verification happens per adapter PR at the pinned model revision
   (candidates declared in the plan: MoritzLaurer/DeBERTa-v3-base-mnli-
   fever-anli (MIT), cross-encoder/nli-deberta-v3-xsmall, BAAI/
   bge-small-en-v1.5 (MIT), cross-encoder/ms-marco-MiniLM-L6-v2) - no
   model is fetched, hosted or paid for by this module. RSS/memory-fit
   claims belong to the adapter PRs and must be MEASURED, not estimated.
9. READ-ONLY, NOT MOUNTED: no action paths.

## Files

- models.py - ProviderConfig (+from_env), CircuitBreaker, NoneProvider,
  FunctionProvider, get_provider, guarded_call.
- test_models.py - 61 tests: config/from_env validation, breaker state
  machine (fake clock), provider delegation/validation, guarded-call
  fallback/timeout/no-retry semantics, fail-closed misbehaving-backend
  probes, backend BaseException failure-path policy probes.
