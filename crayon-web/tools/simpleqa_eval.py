"""SimpleQA brain eval: score Crayon's brain route on a public factuality set.

Loads questions from the public SimpleQA dataset (OpenEvals/SimpleQA) via the
HuggingFace datasets-server rows API (no heavy deps), asks the brain through
llm.plan()/build_or()/build_model() - the same route the app uses - and grades
answers with a strict normalized string match.

HONEST SCOPE: string-match grading is a LOWER BOUND on accuracy - a correct
answer phrased differently scores as wrong. Report results as
"SimpleQA string-match lower bound", never as the model's true score.
Rate limits: the free Gemini/OpenRouter quotas cap how large a run can be;
use --limit and --sleep to stay inside them.

Usage (needs GEMINI_API_KEY / OPENROUTER_API_KEY in env, like the app):
    python -m tools.simpleqa_eval --limit 50 --sleep 1.5 --out /tmp/simpleqa.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import string
import sys
import time
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm  # noqa: E402

DATASET_ROWS_URL = (
    "https://datasets-server.huggingface.co/rows"
    "?dataset=OpenEvals%2FSimpleQA&config=default&split=test&offset={offset}&length={length}"
)

QA_PROMPT = (
    "Answer the following question with a short, exact factual answer - a name, "
    "date, number, or phrase, nothing else. If you do not know, say exactly: I don't know.\n\n"
    "Question: {q}"
)


def fetch_questions(offset: int = 0, length: int = 100, timeout: int = 30):
    """Pull SimpleQA rows from the HF datasets-server (public, no auth)."""
    url = DATASET_ROWS_URL.format(offset=offset, length=length)
    req = urllib.request.Request(url, headers={"User-Agent": "crayon-simpleqa-eval"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.load(r)
    out = []
    for row in data.get("rows", []):
        item = row.get("row", {})
        meta = item.get("metadata") or {}
        out.append({
            "question": item.get("problem", ""),
            "answer": item.get("answer", ""),
            "topic": meta.get("topic", ""),
        })
    return out


_ARTICLES = re.compile(r"\b(a|an|the)\b", re.UNICODE)


def normalize(text: str) -> str:
    """Standard SQuAD-style normalization for short answers."""
    text = text.lower()
    text = "".join(ch for ch in text if ch not in set(string.punctuation))
    text = _ARTICLES.sub(" ", text)
    return " ".join(text.split())


def grade(prediction: str, reference: str) -> str:
    """CORRECT / WRONG / NOT_ATTEMPTED by normalized string match.

    Deliberately strict: a paraphrase that doesn't contain the reference string
    scores WRONG, so the metric is a lower bound. NOT_ATTEMPTED tracks honest
    refusals - a model that says 'I don't know' instead of guessing is not
    penalized as a hallucination in the breakdown.
    """
    p = normalize(prediction)
    r = normalize(reference)
    if not p or "i dont know" in p or "i do not know" in p:
        return "NOT_ATTEMPTED"
    if r and (r in p or p in r):
        return "CORRECT"
    return "WRONG"


def ask_brain(question: str) -> str:
    """Route one question through the app's own plan/build chain (sync wrapper)."""
    import asyncio

    async def _run() -> str:
        last = None
        for kind, nm, _lab in llm.plan(question):
            try:
                mdl = llm.build_or(nm) if kind == "or" else llm.build_model(name=nm)
                r = await mdl.ainvoke(llm.to_messages([], QA_PROMPT.format(q=question), False))
                return r.content if isinstance(r.content, str) else llm.chunk_text(r)
            except Exception as e:  # next step in the plan
                last = e
                if kind == "or" or llm.provider() == "openrouter":
                    llm.invalidate_free_cache()
        raise last if last else RuntimeError("no model in plan")

    return asyncio.run(_run())


def run(limit: int, offset: int, sleep_s: float, out_path: str):
    rows = fetch_questions(offset=offset, length=limit)
    results = []
    counts = {"CORRECT": 0, "WRONG": 0, "NOT_ATTEMPTED": 0}
    for i, item in enumerate(rows):
        pred = ask_brain(item["question"])
        g = grade(pred, item["answer"])
        counts[g] += 1
        results.append({**item, "prediction": pred, "grade": g})
        print(f"[{i + 1}/{len(rows)}] {g:13s} {item['question'][:70]}", flush=True)
        if sleep_s:
            time.sleep(sleep_s)
    n = len(results) or 1
    summary = {
        "dataset": "OpenEvals/SimpleQA (test)",
        "offset": offset, "limit": limit, "n": len(results),
        "metric": "normalized string-match LOWER BOUND (paraphrases score wrong)",
        "counts": counts,
        "accuracy_lb": round(counts["CORRECT"] / n, 4),
        "not_attempted_rate": round(counts["NOT_ATTEMPTED"] / n, 4),
        "provider": llm.provider(),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    with open(out_path, "w") as f:
        json.dump({"summary": summary, "results": results}, f, indent=1)
    print(json.dumps(summary, indent=1))
    return summary


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--offset", type=int, default=0)
    ap.add_argument("--sleep", type=float, default=1.5)
    ap.add_argument("--out", default="simpleqa_results.json")
    a = ap.parse_args()
    run(a.limit, a.offset, a.sleep, a.out)
