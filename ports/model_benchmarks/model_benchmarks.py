"""ports/model_benchmarks - dev-only benchmark harness for the model series.

Honest, reproducible numbers for the ports/models adapters: citation
support (ports/find_support_nli) and personal retrieval
(ports/find_personal_semantic). The harness runs LABELED probe sets
through a support_fn / retrieval callable and computes standard metrics.
It ships probe sets and metric code only: model endpoints are
integration-injected callables, so a benchmark run can use the
deterministic baseline, a local model, or (in dev, within free hosted
credits) a hosted endpoint - this module itself never touches the
network, downloads no weights and makes no paid calls.

REUSE BOUNDARY (not a parallel stack): verdict logic stays in
ports/find_support_nli / ports/find (the harness CALLS a support_fn, it
does not re-implement one); retrieval logic stays in
ports/find_personal_semantic / ports/find_personal. Metric computation
and probe-set validation live here alone.

Design invariants:

1. HONEST NUMBERS ONLY: metrics are computed from recorded per-probe
   outcomes; the report shows probe counts, every metric's definition
   and the exact threshold/config used. No aggregation hides a failing
   slice: per-slice breakdowns are always emitted.
2. LABELED PROBES, VALIDATED: every probe carries an expected label;
   malformed probes (missing/unknown labels, empty texts, duplicate
   ids) refuse the whole run with zero effects.
3. DETERMINISM: same probes + same callable behavior -> identical
   report; probe order never affects metrics.
4. FAIL-CLOSED SCORING: a callable exception on a probe is recorded as
   an error outcome for that probe (counted separately, never silently
   treated as correct), and the run continues.
5. READ-ONLY, DEV-ONLY: not mounted; no network, no downloads, no
   state kept between runs.
"""

import os
import sys

# Provider seam is owned by ports/models (same cross-port pattern as
# ports/find_track -> ports/find/urlcanon.py).
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "models"))

from models import ModelsError

CITATION_LABELS = ("supported", "unsupported")
METRIC_DEFS = {
    "citation_precision": "of claims the gate verified, fraction labeled supported",
    "citation_recall": "of claims labeled supported, fraction the gate verified",
    "citation_false_verify_rate": "of claims labeled unsupported, fraction wrongly verified",
    "retrieval_recall_at_k": "of queries, fraction whose expected record is in the top k",
    "retrieval_mrr": "mean reciprocal rank of the expected record",
}


class BenchmarkError(ModelsError):
    """Invalid probe set, configuration or callable."""


def _check_str(v, name):
    if not isinstance(v, str) or not v.strip():
        raise BenchmarkError("%s must be a non-empty string" % name)
    return v


def validate_citation_probes(probes):
    """Each probe: {"pid", "claim", "content", "label"}."""
    if not isinstance(probes, (list, tuple)) or not probes:
        raise BenchmarkError("probes must be a non-empty list")
    seen = set()
    out = []
    for i, p in enumerate(probes):
        if not isinstance(p, dict):
            raise BenchmarkError("probe %d must be a mapping" % i)
        pid = _check_str(p.get("pid"), "probe %d pid" % i)
        if pid in seen:
            raise BenchmarkError("duplicate probe id %r" % pid)
        seen.add(pid)
        claim = _check_str(p.get("claim"), "probe %r claim" % pid)
        content = _check_str(p.get("content"), "probe %r content" % pid)
        label = p.get("label")
        if label not in CITATION_LABELS:
            raise BenchmarkError("probe %r label must be one of %s"
                                 % (pid, ", ".join(CITATION_LABELS)))
        out.append({"pid": pid, "claim": claim, "content": content,
                    "label": label})
    return out


def validate_retrieval_probes(probes):
    """Each probe: {"pid", "query", "records": [{"rid", "text"}...],
    "expected_rid"}."""
    if not isinstance(probes, (list, tuple)) or not probes:
        raise BenchmarkError("probes must be a non-empty list")
    seen = set()
    out = []
    for i, p in enumerate(probes):
        if not isinstance(p, dict):
            raise BenchmarkError("probe %d must be a mapping" % i)
        pid = _check_str(p.get("pid"), "probe %d pid" % i)
        if pid in seen:
            raise BenchmarkError("duplicate probe id %r" % pid)
        seen.add(pid)
        query = _check_str(p.get("query"), "probe %r query" % pid)
        records = p.get("records")
        if not isinstance(records, (list, tuple)) or not records:
            raise BenchmarkError("probe %r needs a non-empty records list" % pid)
        rids = set()
        recs = []
        for j, r in enumerate(records):
            if not isinstance(r, dict):
                raise BenchmarkError("probe %r record %d must be a mapping" % (pid, j))
            rid = _check_str(r.get("rid"), "probe %r record %d rid" % (pid, j))
            if rid in rids:
                raise BenchmarkError("probe %r duplicate record id %r" % (pid, rid))
            rids.add(rid)
            recs.append({"rid": rid,
                         "text": _check_str(r.get("text"),
                                            "probe %r record %r text" % (pid, rid))})
        expected = _check_str(p.get("expected_rid"), "probe %r expected_rid" % pid)
        if expected not in rids:
            raise BenchmarkError("probe %r expected_rid %r not in records"
                                 % (pid, expected))
        out.append({"pid": pid, "query": query, "records": recs,
                    "expected_rid": expected})
    return out


def run_citation_benchmark(support_fn, probes):
    """Run labeled citation probes through support_fn(claim, content).

    Returns a report dict: per-probe outcomes, totals, precision /
    recall / false-verify-rate and per-label slices. Callable errors are
    recorded as outcome "error" and counted in errors, never as correct.
    """
    if not callable(support_fn):
        raise BenchmarkError("support_fn must be callable")
    probes = validate_citation_probes(probes)
    outcomes = []
    for p in probes:
        try:
            verdict = support_fn(p["claim"], p["content"])
            if not isinstance(verdict, bool):
                outcomes.append({"pid": p["pid"], "label": p["label"],
                                 "outcome": "error"})
                continue
            predicted = "supported" if verdict else "unsupported"
            outcomes.append({"pid": p["pid"], "label": p["label"],
                             "outcome": "correct" if predicted == p["label"]
                                        else ("false_verify"
                                              if verdict else "missed_support")})
        except Exception:
            outcomes.append({"pid": p["pid"], "label": p["label"],
                             "outcome": "error"})
    except_count = sum(1 for o in outcomes if o["outcome"] == "error")
    labeled_supported = [o for o in outcomes if o["label"] == "supported"]
    labeled_unsupported = [o for o in outcomes if o["label"] == "unsupported"]
    true_pos = sum(1 for o in labeled_supported if o["outcome"] == "correct")
    false_pos = sum(1 for o in labeled_unsupported if o["outcome"] == "false_verify")
    n_verified = true_pos + false_pos
    report = {
        "kind": "citation",
        "probes": len(probes),
        "outcomes": outcomes,
        "errors": except_count,
        "true_positives": true_pos,
        "false_positives": false_pos,
        "citation_precision": (true_pos / n_verified) if n_verified else None,
        "citation_recall": (true_pos / len(labeled_supported))
                             if labeled_supported else None,
        "citation_false_verify_rate": (false_pos / len(labeled_unsupported))
                                      if labeled_unsupported else None,
        "slices": {
            "supported": {"count": len(labeled_supported),
                          "verified": true_pos},
            "unsupported": {"count": len(labeled_unsupported),
                            "wrongly_verified": false_pos},
        },
        "metric_definitions": dict(METRIC_DEFS),
    }
    return report


def run_retrieval_benchmark(find_fn, probes, k=10):
    """Run labeled retrieval probes. find_fn(query, records) must return
    an ordered list of rids (best first). k bounds recall@k and must be
    a positive int. Errors are recorded per probe and counted."""
    if not callable(find_fn):
        raise BenchmarkError("find_fn must be callable")
    if isinstance(k, bool) or not isinstance(k, int) or k < 1:
        raise BenchmarkError("k must be a positive int")
    probes = validate_retrieval_probes(probes)
    outcomes = []
    for p in probes:
        try:
            ranked = find_fn(p["query"], [dict(r) for r in p["records"]])
            if not isinstance(ranked, (list, tuple)) \
                    or any(not isinstance(r, str) for r in ranked):
                outcomes.append({"pid": p["pid"], "outcome": "error",
                                 "rank": None})
                continue
            # round-9 (review 5480639560): an invalid ranking is an
            # ERROR, never a score - duplicate rids or rids outside the
            # probe's record set would otherwise fabricate recall@1/MRR
            known = {r["rid"] for r in p["records"]}
            if len(set(ranked)) != len(ranked) \
                    or any(r not in known for r in ranked):
                outcomes.append({"pid": p["pid"], "outcome": "error",
                                 "rank": None})
                continue
            try:
                rank = list(ranked).index(p["expected_rid"]) + 1
            except ValueError:
                rank = None
            outcomes.append({"pid": p["pid"], "outcome": "ok", "rank": rank})
        except Exception:
            outcomes.append({"pid": p["pid"], "outcome": "error",
                             "rank": None})
    ok = [o for o in outcomes if o["outcome"] == "ok"]
    errors = len(outcomes) - len(ok)
    hits = sum(1 for o in ok if o["rank"] is not None and o["rank"] <= k)
    rr = [1.0 / o["rank"] for o in ok if o["rank"] is not None]
    return {
        "kind": "retrieval",
        "probes": len(probes),
        "k": k,
        "outcomes": outcomes,
        "errors": errors,
        "retrieval_recall_at_k": (hits / len(probes)) if probes else None,
        "retrieval_mrr": (sum(rr) / len(probes)) if probes else None,
        "metric_definitions": dict(METRIC_DEFS),
    }


def format_report(report, config_note):
    """Plain-text rendering for pasting into a PR or dev log. Every
    number is traceable to the outcomes list in the same report."""
    if not isinstance(report, dict) or report.get("kind") not in ("citation", "retrieval"):
        raise BenchmarkError("report must be a run_*_benchmark result")
    _check_str(config_note, "config_note")
    lines = []
    lines.append("# %s benchmark - %s" % (report["kind"].capitalize(),
                                          config_note))
    lines.append("probes: %d, errors: %d" % (report["probes"],
                                             report["errors"]))
    # round-9 (review 5480639560): coverage is explicit and errors are
    # broken out per label - a failing slice can never hide inside an
    # aggregate, and false-verify is reported apart from errors
    answered = report["probes"] - report["errors"]
    lines.append("coverage: %d/%d answered (errors excluded from correct"
                 " counts, included in denominators)"
                 % (answered, report["probes"]))
    if report["kind"] == "citation":
        for key in ("citation_precision", "citation_recall",
                    "citation_false_verify_rate"):
            v = report[key]
            lines.append("%s: %s" % (key, "n/a" if v is None else "%.4f" % v))
        lines.append("slices: supported %d/%d verified; unsupported %d/%d wrongly verified"
                     % (report["slices"]["supported"]["verified"],
                        report["slices"]["supported"]["count"],
                        report["slices"]["unsupported"]["wrongly_verified"],
                        report["slices"]["unsupported"]["count"]))
        for label in ("supported", "unsupported"):
            lo = [o for o in report["outcomes"] if o["label"] == label]
            le = sum(1 for o in lo if o["outcome"] == "error")
            lines.append("label errors: %s %d/%d" % (label, le, len(lo)))
        lines.append("per-probe outcomes:")
        for o in report["outcomes"]:
            lines.append("  %s: %s (label=%s)"
                         % (o["pid"], o["outcome"], o["label"]))
    else:
        for key in ("retrieval_recall_at_k", "retrieval_mrr"):
            v = report[key]
            lines.append("%s: %s" % (key, "n/a" if v is None else "%.4f" % v))
        lines.append("per-probe outcomes:")
        for o in report["outcomes"]:
            rank = "none" if o["rank"] is None else str(o["rank"])
            lines.append("  %s: %s rank=%s" % (o["pid"], o["outcome"], rank))
    lines.append("metric definitions: "
                 + "; ".join("%s = %s" % kv
                             for kv in report["metric_definitions"].items()))
    return "\n".join(lines)
