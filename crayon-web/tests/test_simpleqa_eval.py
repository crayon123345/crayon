"""Offline tests for the SimpleQA eval runner (mocked dataset + brain)."""
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from tools import simpleqa_eval as sq  # noqa: E402


def test_normalize():
    assert sq.normalize("The Eiffel Tower!") == "eiffel tower"
    assert sq.normalize("  New   York,  USA ") == "new york usa"


def test_grade_correct_substring_both_ways():
    assert sq.grade("Paris", "Paris") == "CORRECT"
    assert sq.grade("The answer is Paris.", "Paris") == "CORRECT"
    assert sq.grade("1969", "July 1969") == "CORRECT"


def test_grade_wrong_and_refusal():
    assert sq.grade("London", "Paris") == "WRONG"
    assert sq.grade("I don't know.", "Paris") == "NOT_ATTEMPTED"
    assert sq.grade("", "Paris") == "NOT_ATTEMPTED"


def test_grade_paraphrase_is_lower_bound():
    # correct in meaning, wrong by string match - documents the metric's honesty
    assert sq.grade("Neil Armstrong", "Armstrong") == "CORRECT"
    assert sq.grade("the sixteenth president", "Abraham Lincoln") == "WRONG"


def test_fetch_questions_parsing(monkeypatch):
    import io as _io
    payload = {"rows": [
        {"row": {"problem": "Q1?", "answer": "A1", "metadata": {"topic": "science"}}},
        {"row": {"problem": "Q2?", "answer": "A2", "metadata": {}}},
    ]}

    class FakeResp:
        def __enter__(self): return _io.BytesIO(json.dumps(payload).encode())
        def __exit__(self, *a): return False

    monkeypatch.setattr(sq.urllib.request, "urlopen", lambda req, timeout=30: FakeResp())
    rows = sq.fetch_questions(offset=0, length=2)
    assert rows == [
        {"question": "Q1?", "answer": "A1", "topic": "science"},
        {"question": "Q2?", "answer": "A2", "topic": ""},
    ]


def test_run_end_to_end_mocked(tmp_path, monkeypatch):
    rows = [
        {"question": "Capital of France?", "answer": "Paris", "topic": "geo"},
        {"question": "2+2?", "answer": "4", "topic": "math"},
        {"question": "Obscure fact?", "answer": "x", "topic": "misc"},
    ]
    monkeypatch.setattr(sq, "fetch_questions", lambda offset=0, length=100: rows)
    answers = iter(["Paris", "5", "I don't know."])
    monkeypatch.setattr(sq, "ask_brain", lambda q: next(answers))
    out = tmp_path / "res.json"
    summary = sq.run(limit=3, offset=0, sleep_s=0, out_path=str(out))
    assert summary["counts"] == {"CORRECT": 1, "WRONG": 1, "NOT_ATTEMPTED": 1}
    assert summary["n"] == 3
    saved = json.loads(out.read_text())
    assert saved["summary"]["metric"].startswith("normalized string-match LOWER BOUND")
    assert len(saved["results"]) == 3
