"""ports/find_support_nli - NLI support_fn adapter for ports/find.

A candidate production support_fn for ports/find.Find.verify: it keeps the
deterministic reference gate as the base verdict and adds a GUARDED semantic
upgrade path through a pretrained NLI model (natural language inference:
does this content sentence entail this claim). The model can only help
VERIFY a paraphrased claim the verbatim gate cannot quote; it can never
verify a claim that fails the gate's numeric, negation/hedge or
minimum-evidence safeguards, and it never grants permission for anything.

REUSE BOUNDARY (not a parallel stack): ports/find owns the citation
decisions and every gate safeguard - sentence boundaries, tokenization,
exact numeric tokens, negation/hedge sets, minimum evidence. This adapter
IMPORTS those internals (same pattern as ports/find_track importing
ports/find/urlcanon.py) and adds no safeguard logic of its own.
ports/models owns the provider seam; every model call here goes through
models.guarded_call, so a cold, down, slow or misbehaving model endpoint
answers with the deterministic fallback. The model itself (e.g. a
DeBERTa-v3 NLI model) is an integration-injected callable - this module
ships no weights, downloads nothing and touches no network.

Design invariants:

1. DETERMINISTIC BASE FIRST: a claim the reference gate verifies returns
   True with ZERO model calls. The model is consulted only for claims the
   verbatim gate cannot verify.
2. MODEL CAN ONLY ADD VERDICTS THE SAFEGUARDS ALLOW: a semantic verify
   requires (a) the claim to carry the gate's minimum evidence, (b) every
   numeric claim token to appear EXACTLY in the candidate sentence
   (currency, sign, % and separators load-bearing, same tokens as the
   gate), (c) negation/hedge parity between claim and sentence (the
   gate's own sets), and (d) NLI entailment >= threshold. Paraphrase is
   allowed ONLY through this path.
3. FAIL-CLOSED ON MODEL FAILURE: timeout, exception, open breaker,
   disabled/unavailable capability or an out-of-contract model response
   all yield no semantic verdict - the citation drops exactly as the
   deterministic default would drop it. One model attempt per candidate
   sentence, no retries.
4. BOUNDED MODEL USE: at most MAX_NLI_PREMISES candidate sentences are
   scored per claim, in content order; further sentences are simply not
   scored (fail-closed direction, never a substitute verdict).
   A backend contradiction score >= threshold vetoes the verdict even
   when entailment passes (round-9); the premise/hypothesis text the
   model sees is the gate's token-joined lowercase form (documented
   limitation: casing/punctuation are already normalized away by the
   gate's tokenizer, and numeric exactness is checked on the same
   tokens).
5. NO STATE: the adapter holds no verdicts, caches nothing and creates
   no state on reads; breaker state lives in the caller-owned
   models.CircuitBreaker.
6. READ-ONLY, NOT MOUNTED: no network, no downloads, no action paths.
"""

import os
import sys

# Gate safeguards and the deterministic base verdict are owned by
# ports/find; the provider seam is owned by ports/models. Same cross-port
# import pattern as ports/find_track -> ports/find/urlcanon.py.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "find"))
sys.path.insert(0, os.path.join(_HERE, "..", "models"))

from find import (reference_support, MIN_EVIDENCE_TOKENS, _gate_tokens,
                  _gate_sent_tokens, _evidence_tokens, _NEG, _NEG_STEMS,
                  _HEDGE)
from models import (ModelsError, CapabilityUnavailable, ProviderConfig,
                    CircuitBreaker, guarded_call)

MAX_NLI_PREMISES = 25


class FindSupportNliError(ModelsError):
    """Adapter wiring error: invalid arguments to make_support_fn."""


def _numeric_tokens(tokens):
    return [t for t in tokens if any(c.isdigit() for c in t)]


def _marks(tokens):
    return set(tokens) & (_NEG | _NEG_STEMS | _HEDGE)


def make_support_fn(config, breaker, provider, threshold=None):
    """Build a support_fn(claim_text, content) -> bool for Find.verify.

    config: models.ProviderConfig (capability "nli" must be enabled for
    any semantic verdict; with it disabled the result is exactly the
    deterministic gate's). breaker: caller-owned models.CircuitBreaker.
    provider: any models provider exposing capabilities() and nli().
    threshold: entailment minimum in [0, 1]; default
    config.nli_entailment_min."""
    if not isinstance(config, ProviderConfig):
        raise FindSupportNliError("config must be a ProviderConfig")
    if not isinstance(breaker, CircuitBreaker):
        raise FindSupportNliError("breaker must be a CircuitBreaker")
    if not callable(getattr(provider, "capabilities", None)) or \
            not callable(getattr(provider, "nli", None)):
        raise FindSupportNliError("provider must expose capabilities() and nli()")
    if threshold is None:
        threshold = config.nli_entailment_min
    if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) \
            or not (0.0 <= float(threshold) <= 1.0):
        raise FindSupportNliError("threshold must be a number in [0, 1]")
    threshold = float(threshold)

    def _nli_scores(premise_text, hypothesis_text):
        try:
            raw = provider.nli(premise_text, hypothesis_text)
        except CapabilityUnavailable:
            raise
        except ModelsError as exc:
            # A backend must signal failures with ordinary exceptions or
            # CapabilityUnavailable; a ModelsError escaping the backend is
            # treated as a backend failure (drop path), never as a caller
            # bug that would crash the verifying find batch.
            raise CapabilityUnavailable("backend raised ModelsError: %s" % exc)
        if not isinstance(raw, dict):
            raise CapabilityUnavailable("nli response must be a mapping")
        score = raw.get("entailment")
        if isinstance(score, bool) or not isinstance(score, (int, float)) \
                or not (0.0 <= float(score) <= 1.0):
            raise CapabilityUnavailable("nli response needs entailment in [0, 1]")
        # Optional contradiction score: when the backend supplies one it
        # is validated just as strictly, and a contradiction at or above
        # the entailment threshold vetoes the verdict - an entailed-but-
        # contradicted premise can never verify (round-9 review patch).
        contra = raw.get("contradiction")
        if contra is not None:
            if isinstance(contra, bool) or not isinstance(contra, (int, float)) \
                    or not (0.0 <= float(contra) <= 1.0):
                raise CapabilityUnavailable(
                    "nli contradiction must be in [0, 1] when present")
            contra = float(contra)
        return float(score), contra

    def support_fn(claim_text, content):
        if not isinstance(claim_text, str) or not isinstance(content, str):
            return False
        # 1. Deterministic base: verbatim quote evidence needs no model.
        if reference_support(claim_text, content):
            return True
        # 2. Guarded semantic upgrade.
        claim_tokens = _gate_tokens(claim_text)
        if len(_evidence_tokens(claim_text)) < MIN_EVIDENCE_TOKENS:
            return False
        claim_numbers = _numeric_tokens(claim_tokens)
        claim_marks = _marks(claim_tokens)
        hypothesis = " ".join(claim_tokens)
        if not hypothesis:
            return False
        scored = 0
        for sent_tokens in _gate_sent_tokens(content):
            if scored >= MAX_NLI_PREMISES:
                break
            if not sent_tokens:
                continue
            # Gate safeguards, reused - the model cannot override them.
            if _marks(sent_tokens) != claim_marks:
                continue
            sent_token_set = set(sent_tokens)
            if any(n not in sent_token_set for n in claim_numbers):
                continue
            scored += 1
            premise = " ".join(sent_tokens)
            pair = guarded_call(
                config, breaker, provider, "nli",
                lambda p=premise: _nli_scores(p, hypothesis),
                lambda: None)
            if pair is not None and pair[0] >= threshold \
                    and not (pair[1] is not None and pair[1] >= threshold):
                return True
        return False

    return support_fn
