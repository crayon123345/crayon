"""Fail-closed free-only routing guards for llm.py (mocked catalog, no network)."""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import llm  # noqa: E402

LIVE_FREE = {
    "nvidia/nemotron-3-ultra-550b-a55b:free",
    "cohere/north-mini-code:free",
    "openrouter/free",
    "deepseek/deepseek-chat-v3-0324:free",  # hypothetical future free listing
}
PAID = "deepseek/deepseek-chat-v3-0324"


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for k in ("CRAYON_DEEPSEEK_MODEL", "CRAYON_CODE_MODELS", "OPENROUTER_MODEL", "LLM_PROVIDER"):
        monkeypatch.delenv(k, raising=False)
    llm.invalidate_free_cache()


# ---- blocker 1: every OpenRouter constructor is guarded ----

def test_build_or_refuses_when_catalog_down(monkeypatch):
    """Cold/down catalog: constructor must never be reached (RuntimeError, not ModuleNotFoundError)."""
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: None)
    with pytest.raises(RuntimeError, match="unverifiable"):
        llm.build_or("nvidia/nemotron-3-ultra-550b-a55b:free")  # even a pinned model


def test_build_or_refuses_paid_direct(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    with pytest.raises(RuntimeError, match="not zero-price"):
        llm.build_or(PAID)


def test_catalog_goes_paid_between_plan_and_construction(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    planned = llm.plan("fix this python bug")
    assert planned[0][0] == "or"
    # catalog re-check now says the planned model is gone/paid
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: {"cohere/north-mini-code:free"})
    with pytest.raises(RuntimeError, match="not zero-price"):
        llm.build_or(planned[0][1])


def test_catalog_down_code_models_returns_nothing(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: None)
    monkeypatch.setenv("CRAYON_CODE_MODELS", "cohere/north-mini-code:free")
    assert llm.code_models() == []  # pinned metadata is not a spend guarantee


def test_plan_falls_back_to_gemini_when_catalog_down(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    assert all(kind == "gemini" for kind, _, _ in llm.plan("fix this python bug"))


# ---- blocker 2: exact, complete price check ----

def test_price_parser_numeric_zero_accepted():
    assert llm._pricing_is_zero({"prompt": 0, "completion": 0}) is True
    assert llm._pricing_is_zero({"prompt": "0", "completion": "0.0"}) is True


def test_price_parser_underflow_rejected():
    assert llm._pricing_is_zero({"prompt": "1e-999", "completion": "0"}) is False


def test_price_parser_request_fee_rejected():
    assert llm._pricing_is_zero({"prompt": "0", "completion": "0", "request": "0.1"}) is False


def test_price_parser_tiered_overrides_checked():
    tier_paid = {"prompt": "0", "completion": "0",
                 "overrides": [{"min_prompt_tokens": 100000, "prompt": "0.0000005", "completion": "0"}]}
    assert llm._pricing_is_zero(tier_paid) is False
    tier_free = {"prompt": "0", "completion": "0",
                 "overrides": [{"min_prompt_tokens": 100000, "prompt": "0", "completion": "0"}]}
    assert llm._pricing_is_zero(tier_free) is True


def test_price_parser_absent_or_unknown_not_free():
    assert llm._pricing_is_zero(None) is False
    assert llm._pricing_is_zero({}) is False
    assert llm._pricing_is_zero({"prompt": "NaN", "completion": "0"}) is False
    assert llm._pricing_is_zero({"prompt": "Infinity", "completion": "0"}) is False
    assert llm._pricing_is_zero({"prompt": None, "completion": "0"}) is False
    assert llm._pricing_is_zero({"prompt": True, "completion": "0"}) is False


def test_free_model_ids_uses_full_price_check(monkeypatch):
    payload = {"data": [
        {"id": "a/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "b/tokenfree-requestfee", "pricing": {"prompt": "0", "completion": "0", "request": "0.02"}},
        {"id": "c/paid", "pricing": {"prompt": "0.000001", "completion": "0"}},
        {"id": "d/noprice"},
        {"id": "e/numeric-zero", "pricing": {"prompt": 0, "completion": 0}},
    ]}
    import io as _io
    import json as _json

    class FakeResp:
        def __enter__(self): return _io.BytesIO(_json.dumps(payload).encode())
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm._urlreq, "urlopen", lambda req, timeout=10: FakeResp())
    ids = llm.free_model_ids(force=True)
    assert ids == {"a/free", "e/numeric-zero"}


# ---- blocker 3: freshness, invalidation, execution-time protection ----

def test_cache_ttl_expiry(monkeypatch):
    calls = {"n": 0}

    def fake_fetch(req, timeout=10):
        calls["n"] += 1
        import io as _io
        import json as _json

        class FakeResp:
            def __enter__(self): return _io.BytesIO(_json.dumps({"data": []}).encode())
            def __exit__(self, *a): return False
        return FakeResp()

    monkeypatch.setattr(llm._urlreq, "urlopen", fake_fetch)
    llm.invalidate_free_cache()
    t0 = 1000000.0
    monkeypatch.setattr(llm._time, "time", lambda: t0)
    llm.free_model_ids()
    llm.free_model_ids()  # warm
    assert calls["n"] == 1
    monkeypatch.setattr(llm._time, "time", lambda: t0 + llm.OR_CATALOG_TTL_S + 1)
    llm.free_model_ids()  # expired -> refetch
    assert calls["n"] == 2


def test_invalidate_free_cache_forces_refetch(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    llm.invalidate_free_cache()
    assert llm._FREE_CACHE["ids"] is None


def test_execution_time_max_price_body():
    body = llm._or_extra_body()
    assert body == {"provider": {"max_price": {k: "0" for k in
                    ("prompt", "completion", "image", "audio", "request")}}}
    assert llm.OR_CATALOG_TTL_S <= 300


# ---- earlier guard behavior that must still hold ----

def test_paid_deepseek_override_refused(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    monkeypatch.setenv("CRAYON_DEEPSEEK_MODEL", PAID)
    names = llm.code_models()
    assert PAID not in names
    assert names[0] == "nvidia/nemotron-3-ultra-550b-a55b:free"


def test_free_override_accepted_first(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    monkeypatch.setenv("CRAYON_DEEPSEEK_MODEL", "deepseek/deepseek-chat-v3-0324:free")
    assert llm.code_models()[0] == "deepseek/deepseek-chat-v3-0324:free"


def test_pinned_model_turned_paid_is_dropped(monkeypatch):
    shrunk = {"cohere/north-mini-code:free", "openrouter/free"}
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: shrunk)
    assert llm.code_models() == ["cohere/north-mini-code:free", "openrouter/free"]


def test_openrouter_provider_paid_model_raises(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "google/gemini-2.5-flash")  # paid on OR
    with pytest.raises(RuntimeError, match="not zero-price"):
        llm.build_model()


def test_openrouter_provider_catalog_down_raises(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: None)
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    with pytest.raises(RuntimeError, match="unverifiable"):
        llm.build_model()


def test_free_model_ids_network_error_returns_none(monkeypatch):
    def boom(req, timeout=10):
        raise OSError("down")
    monkeypatch.setattr(llm._urlreq, "urlopen", boom)
    assert llm.free_model_ids(force=True) is None


# ---- round 4: complete-base-price admission, raw-JSON boundary, wired invalidation ----

def test_incomplete_pricing_not_admitted():
    # only a zero request fee, no base token prices -> not free
    assert llm._pricing_is_zero({"request": "0"}) is False
    # threshold-only entry -> not free
    assert llm._pricing_is_zero({"min_prompt_tokens": 100}) is False
    # tier present but no top-level base prices -> not free
    assert llm._pricing_is_zero({"overrides": [{"prompt": "0", "completion": "0"}]}) is False
    # tier missing its own base prices -> not free
    assert llm._pricing_is_zero({"prompt": "0", "completion": "0",
                                 "overrides": [{"min_prompt_tokens": 100, "request": "0"}]}) is False
    # complete and zero -> free
    assert llm._pricing_is_zero({"prompt": "0", "completion": "0", "request": "0",
                                 "overrides": [{"min_prompt_tokens": 100, "prompt": "0", "completion": "0"}]}) is True


def test_raw_json_numeric_boundary(monkeypatch):
    """RAW JSON numbers, not strings: 1e-999 must survive transport as a nonzero
    Decimal and be rejected; raw numeric 0 must be accepted."""
    import io as _io
    raw = b'{"data": [' \
          b'{"id": "tiny/raw", "pricing": {"prompt": 1e-999, "completion": 0}}, ' \
          b'{"id": "zero/raw", "pricing": {"prompt": 0, "completion": 0.0}}, ' \
          b'{"id": "fee/raw", "pricing": {"prompt": 0, "completion": 0, "request": 0.02}}' \
          b']}'

    class FakeResp:
        def __enter__(self): return _io.BytesIO(raw)
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm._urlreq, "urlopen", lambda req, timeout=10: FakeResp())
    ids = llm.free_model_ids(force=True)
    assert ids == {"zero/raw"}


def test_twilio_or_failure_invalidates_cache(monkeypatch):
    """Mirrors the review scenario: Twilio path, OpenRouter step fails, Gemini serves.
    The error path must drop the catalog cache so the next plan re-verifies."""
    import types
    import asyncio

    fake_app = types.ModuleType("app")

    class FakeResp2:
        content = "gemini answer"

    class FakeGemini:
        async def ainvoke(self, msgs): return FakeResp2()

    fake_app.model = lambda nm=None: FakeGemini()
    monkeypatch.setitem(sys.modules, "app", fake_app)

    monkeypatch.setattr(llm, "plan", lambda text: [("or", "some/model:free", "m"), ("gemini", "g", "g")])
    monkeypatch.setattr(llm, "to_messages", lambda h, t, m, a=False: [types.SimpleNamespace(content="sys")])
    def boom(name):
        raise RuntimeError("openrouter down")
    monkeypatch.setattr(llm, "build_or", boom)
    llm._FREE_CACHE.update(ts=llm._time.time(), ids={"some/model:free"})  # warm cache

    import twilio_wa
    out = asyncio.run(twilio_wa.think([], "hi"))
    assert out == "gemini answer"
    assert llm._FREE_CACHE["ids"] is None  # invalidated by the failing OR step
