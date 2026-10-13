"""Search backend fallback: _do_search must survive single-engine failures.

Mirrors the live 2026-10-13 probe: ddgs duckduckgo/bing text and
duckduckgo/yahoo news raised DDGSException("No results found.") while
yahoo text and bing news answered. Network is never touched here: a fake
ddgs module is injected into sys.modules (app._do_search imports ddgs
lazily inside the function).
"""

import sys
import types

import pytest

import app


class DDGSException(Exception):
    pass


class FakeDDGS:
    """Records (method, backend) calls; behavior keyed by (method, backend).

    behavior: dict mapping ("text"|"news", backend) to "rows" | "empty" | "raise".
    Default: "raise".
    """

    calls = []
    behavior = {}

    def __init__(self, timeout=None):
        self.timeout = timeout

    def text(self, q, max_results=None, region=None, safesearch=None, backend=None):
        type(self).calls.append(("text", backend))
        return self._act("text", backend, href=True)

    def news(self, q, max_results=None, safesearch=None, backend=None):
        type(self).calls.append(("news", backend))
        return self._act("news", backend, href=False)

    def _act(self, method, backend, href):
        mode = type(self).behavior.get((method, backend), "raise")
        if mode == "raise":
            raise DDGSException("No results found.")
        if mode == "empty":
            return []
        row = {"title": f"{method}-{backend} title", "body": "snippet", "date": "2026-10-13"}
        row["href" if href else "url"] = f"https://example.com/{method}-{backend}"
        return [row]


@pytest.fixture
def fake_ddgs(monkeypatch):
    FakeDDGS.calls = []
    FakeDDGS.behavior = {}
    mod = types.ModuleType("ddgs")
    mod.DDGS = FakeDDGS
    mod.DDGSException = DDGSException
    monkeypatch.setitem(sys.modules, "ddgs", mod)
    return FakeDDGS


def test_text_falls_back_to_second_backend(fake_ddgs):
    fake_ddgs.behavior = {("text", "yahoo"): "rows", ("news", "auto"): "rows"}
    res, ok = app._do_search("q")
    assert ok is True
    assert any(r["url"] == "https://example.com/text-yahoo" for r in res)
    text_backends = [b for m, b in fake_ddgs.calls if m == "text"]
    assert text_backends == list(app._SEARCH_TEXT_BACKENDS)  # tried in order


def test_news_falls_back_to_second_backend(fake_ddgs):
    fake_ddgs.behavior = {("news", "bing"): "rows", ("text", "auto"): "rows"}
    res, ok = app._do_search("q")
    assert ok is True
    assert any(r["url"] == "https://example.com/news-bing" for r in res)


def test_empty_result_triggers_fallback(fake_ddgs):
    fake_ddgs.behavior = {("text", "auto"): "empty", ("text", "yahoo"): "rows",
                          ("news", "auto"): "rows"}
    res, ok = app._do_search("q")
    assert any(r["url"] == "https://example.com/text-yahoo" for r in res)
    text_backends = [b for m, b in fake_ddgs.calls if m == "text"]
    assert text_backends == ["auto", "yahoo"]


def test_all_empty_is_success_with_no_rows(fake_ddgs):
    fake_ddgs.behavior = {("text", "auto"): "empty", ("text", "yahoo"): "empty",
                          ("news", "auto"): "empty", ("news", "bing"): "empty"}
    res, ok = app._do_search("obscure query with no hits anywhere")
    assert res == []
    assert ok is True  # engines answered; a real no-hit query is not an outage


def test_partial_outage_still_ok(fake_ddgs):
    # every news backend raises, text works: partial results, still ok
    fake_ddgs.behavior = {("text", "auto"): "rows"}
    res, ok = app._do_search("q")
    assert ok is True
    assert [r["url"] for r in res] == ["https://example.com/text-auto"]


def test_total_outage_not_ok(fake_ddgs):
    res, ok = app._do_search("q")  # everything raises
    assert res == []
    assert ok is False


def test_rows_normalized_and_deduped(fake_ddgs):
    dup = {"title": "t" * 200, "body": "s" * 400, "date": "2026-10-13"}
    class SameURL(FakeDDGS):
        def _act(self, method, backend, href):
            row = dict(dup)
            row["href" if href else "url"] = "https://example.com/same"
            return [row]
    fake_ddgs.behavior = {("text", "auto"): "rows", ("news", "auto"): "rows"}
    FakeDDGS._act = SameURL._act
    try:
        res, ok = app._do_search("q")
    finally:
        del FakeDDGS._act
    assert ok is True
    assert len(res) == 1  # same URL across families deduped
    assert len(res[0]["title"]) == 140 and len(res[0]["snippet"]) == 300
