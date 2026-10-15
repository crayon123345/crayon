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

VARIANT: make_retrying_support_fn adds two strictly-subtractive safety
behaviors and one recall behavior, benchmarked on the v2 probe set
(80 citation probes) with a local DeBERTa-v3-base NLI model:

- RESTRICTION GUARD: a premise with a scope/quantity restriction the
  claim omits can never verify. Motivation: measured false-verifies on
  partial-entailment traps ("children under five" vs "children";
  "twenty minutes from the beach" vs "near the beach") at EVERY usable
  threshold on both xsmall and base models - no threshold keeps
  false-verify at 0 there, so the fix is structural, not numeric.
- SINGLE RETRY: one extra bounded pass over re-segmented content
  (clauses, sentence pairs) before refusing, recovering claims whose
  evidence is split across or buried inside sentences. The
  MAX_NLI_PREMISES budget is shared across both passes.

make_support_fn behavior is unchanged; the variant is opt-in.
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

# Scope/quantity restriction markers: a premise carrying one the claim omits
# is the classic partial-entailment trap ("children under five" does NOT
# support "children"; "twenty minutes from the beach" does NOT support
# "near the beach"). A model score can never override an unrestricted
# reading of a restricted premise. Used ONLY by the retrying variant.
_RESTRICTION_MARKERS = frozenset({
    "under", "over", "only", "except", "fewer", "below", "above",
    "maximum", "minimum", "barely", "almost", "nearly",
})
_RESTRICTION_PHRASES = ("at least", "at most", "up to", "no more than",
                        "less than", "more than")


def _restriction_gap(premise_tokens, claim_tokens):
    """True when the premise carries a restriction the claim omits."""
    claim_set = set(claim_tokens)
    for m in _RESTRICTION_MARKERS:
        if m in premise_tokens and m not in claim_set:
            return True
    ptext = " ".join(premise_tokens)
    ctext = " ".join(claim_tokens)
    for ph in _RESTRICTION_PHRASES:
        if ph in ptext and ph not in ctext:
            return True
    return False


def _retry_variants(sentences):
    """Re-segmented source for the single retry pass: comma/semicolon
    clauses (evidence buried in a long sentence) and consecutive sentence
    pairs (evidence split across two sentences). Same gate tokens."""
    variants = []
    for sent in sentences:
        clause = []
        for tok in sent:
            if tok in (",", ";", ":") and clause:
                variants.append(clause)
                clause = []
            else:
                clause.append(tok)
        if clause:
            variants.append(clause)
    for i in range(len(sentences) - 1):
        variants.append(sentences[i] + sentences[i + 1])
    return variants


class FindSupportNliError(ModelsError):
    """Adapter wiring error: invalid arguments to make_support_fn."""


def _numeric_tokens(tokens):
    return [t for t in tokens if any(c.isdigit() for c in t)]


def _marks(tokens):
    return set(tokens) & (_NEG | _NEG_STEMS | _HEDGE)


_NUM_WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
    "eleven": 11, "twelve": 12, "thirteen": 13, "fourteen": 14,
    "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18,
    "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80,
    "ninety": 90, "dozen": 12,
}
# Canonical-unit factors: value * factor == canonical amount. Time only -
# weight/distance conversion is not claimed.
_TIME_UNITS = {
    "second": 1, "seconds": 1,
    "minute": 60, "minutes": 60,
    "hour": 3600, "hours": 3600,
    "day": 86400, "days": 86400,
    "week": 604800, "weeks": 604800,
}


def _parse_number(tok):
    """Digits or a single number word -> float, else None."""
    try:
        return float(tok)
    except ValueError:
        pass
    return _NUM_WORDS.get(tok)


def _quantities(tokens):
    """(canonical_seconds_or_None, raw_value, unit_or_None) for every
    number token; unit attaches only to an immediately following unit
    word. Exact equality after conversion - never approximate."""
    out = []
    for i, t in enumerate(tokens):
        v = _parse_number(t)
        if v is None:
            continue
        unit = tokens[i + 1] if i + 1 < len(tokens) else None
        factor = _TIME_UNITS.get(unit) if unit else None
        out.append((v * factor if factor else None, v, unit if factor else None))
    return out


def _numeric_match(claim_tokens, sent_token_set, sent_tokens):
    """Numeric safeguard with word/digit + exact time-unit conversion.
    Every claim quantity must appear in the premise: time quantities
    match on exact canonical seconds (120 minutes == 2 hours);
    plain numbers match on normalized value (two == 2); symbol-bearing
    numeric tokens ($200, 9:40, 2bhk) fall back to the gate's exact-token
    check. Same fail-closed direction as the gate: it can only refuse,
    never verify by itself."""
    sent_quantities = _quantities(sent_tokens)
    sent_values = {v for _, v, _ in sent_quantities}
    sent_timed = {c for c, _, u in sent_quantities if u}
    for i, t in enumerate(claim_tokens):
        if t not in _NUM_WORDS and not any(c.isdigit() for c in t):
            continue
        v = _parse_number(t)
        if v is None:
            if t not in sent_token_set:
                return False
            continue
        unit = claim_tokens[i + 1] if i + 1 < len(claim_tokens) else None
        factor = _TIME_UNITS.get(unit) if unit else None
        if factor:
            if v * factor not in sent_timed:
                return False
        elif any(c.isdigit() for c in t):
            # Digit tokens keep gate strictness (normalized value or exact).
            if v not in sent_values and t not in sent_token_set:
                return False
        # Unitless number WORDS keep the gate's vacuous behavior: the
        # exact-token check never saw them, and counting semantics
        # ("three times a week" == "monday wednesday and friday") are the
        # model's call, not the gate's.
    return True


def _nli_scores(provider, premise_text, hypothesis_text):
    """Module-level twin of the nested scorer in make_support_fn: same
    backend call, same ModelsError wrapping, same strict validation."""
    try:
        raw = provider.nli(premise_text, hypothesis_text)
    except CapabilityUnavailable:
        raise
    except ModelsError as exc:
        raise CapabilityUnavailable("backend raised ModelsError: %s" % exc)
    return _validate_nli_response(raw)


def _validate_nli_response(raw):
    """Contract-check one backend response; returns (entailment, contra)."""
    if not isinstance(raw, dict):
        raise CapabilityUnavailable("nli response must be a mapping")
    score = raw.get("entailment")
    if isinstance(score, bool) or not isinstance(score, (int, float)) \
            or not (0.0 <= float(score) <= 1.0):
        raise CapabilityUnavailable("nli response needs entailment in [0, 1]")
    score = float(score)
    contra = raw.get("contradiction")
    if contra is not None:
        if isinstance(contra, bool) or not isinstance(contra, (int, float)) \
                or not (0.0 <= float(contra) <= 1.0):
            raise CapabilityUnavailable(
                "nli contradiction must be in [0, 1] when present")
        contra = float(contra)
    return score, contra


def _scan(config, breaker, provider, threshold, hypothesis, claim_numbers,
          claim_marks, premise_variants, scored, restriction_guard):
    """One bounded pass over premise variants; returns (verdict, scored).
    Never raises on backend failure - a bad answer is no answer."""
    for sent_tokens in premise_variants:
        if scored >= MAX_NLI_PREMISES:
            break
        if not sent_tokens:
            continue
        # Gate safeguards, reused - the model cannot override them.
        if _marks(sent_tokens) != claim_marks:
            continue
        sent_token_set = set(sent_tokens)
        if restriction_guard:
            if not _numeric_match(hypothesis.split(), sent_token_set,
                                  sent_tokens):
                continue
            if _restriction_gap(sent_tokens, hypothesis.split()):
                continue
        elif any(n not in sent_token_set for n in claim_numbers):
            continue
        scored += 1
        premise = " ".join(sent_tokens)
        pair = guarded_call(
            config, breaker, provider, "nli",
            lambda p=premise: _nli_scores(provider, p, hypothesis),
            lambda: None)
        if pair is not None and pair[0] >= threshold \
                and not (pair[1] is not None and pair[1] >= threshold):
            return True, scored
    return False, scored


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


def make_retrying_support_fn(config, breaker, provider, threshold=None):
    """Variant of make_support_fn that retries ONCE with a re-segmented
    source before refusing, and refuses restricted-premise matches.

    Same constructor contract as make_support_fn. Differences:

    1. RESTRICTION GUARD: a candidate premise carrying a scope/quantity
       restriction marker the claim omits ("under", "only", "at least",
       ...) is never a verification - the classic partial-entailment
       trap ("children under five" does not support "children"). The
       guard can only REMOVE model verdicts, never add one.
    2. SINGLE RETRY: when the sentence-level pass finds no verdict, one
       bounded second pass runs over re-segmented content - comma/
       semicolon clauses (evidence buried in a long sentence) and
       consecutive sentence pairs (evidence split across two
       sentences). The MAX_NLI_PREMISES budget is SHARED across both
       passes, so worst-case model use stays bounded.
    3. FAIL-CLOSED UNCHANGED: backend failure in either pass is no
       verdict; the deterministic base still answers first with zero
       model calls.
    """
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

    def support_fn(claim_text, content):
        if not isinstance(claim_text, str) or not isinstance(content, str):
            return False
        # 1. Deterministic base: verbatim quote evidence needs no model.
        if reference_support(claim_text, content):
            return True
        # 2. Guarded semantic upgrade with restriction guard + one retry.
        claim_tokens = _gate_tokens(claim_text)
        if len(_evidence_tokens(claim_text)) < MIN_EVIDENCE_TOKENS:
            return False
        claim_numbers = _numeric_tokens(claim_tokens)
        claim_marks = _marks(claim_tokens)
        hypothesis = " ".join(claim_tokens)
        if not hypothesis:
            return False
        sentences = _gate_sent_tokens(content)
        verdict, scored = _scan(
            config, breaker, provider, threshold, hypothesis,
            claim_numbers, claim_marks, sentences, 0, True)
        if verdict:
            return True
        # 3. Single retry over re-segmented content before refusing.
        verdict, scored = _scan(
            config, breaker, provider, threshold, hypothesis,
            claim_numbers, claim_marks, _retry_variants(sentences),
            scored, True)
        return verdict

    return support_fn
