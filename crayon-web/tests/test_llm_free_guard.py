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


def test_catalog_down_drops_env_keeps_pinned(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: None)
    monkeypatch.setenv("CRAYON_DEEPSEEK_MODEL", "deepseek/deepseek-chat-v3-0324:free")
    monkeypatch.setenv("CRAYON_CODE_MODELS", "cohere/north-mini-code:free")
    assert llm.code_models() == list(llm.PINNED_FREE_CODE_MODELS)


def test_pinned_model_turned_paid_is_dropped(monkeypatch):
    shrunk = {"cohere/north-mini-code:free", "openrouter/free"}
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: shrunk)
    names = llm.code_models()
    assert "nvidia/nemotron-3-ultra-550b-a55b:free" not in names
    assert names == ["cohere/north-mini-code:free", "openrouter/free"]


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


def test_openrouter_provider_free_model_passes_guard(monkeypatch):
    monkeypatch.setattr(llm, "free_model_ids", lambda force=False: set(LIVE_FREE))
    monkeypatch.setenv("LLM_PROVIDER", "openrouter")
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-test")
    monkeypatch.setenv("OPENROUTER_MODEL", "openrouter/free")
    try:
        m = llm.build_model()
    except ModuleNotFoundError:
        pytest.skip("langchain_openai not installed in this env")
    assert m.model_name == "openrouter/free"


def test_free_model_ids_parses_zero_price(monkeypatch):
    payload = {"data": [
        {"id": "a/free", "pricing": {"prompt": "0", "completion": "0"}},
        {"id": "b/paid", "pricing": {"prompt": "0.000001", "completion": "0"}},
        {"id": "c/paid2", "pricing": {"prompt": "0", "completion": "0.000001"}},
        {"id": "d/noprice"},
    ]}
    import io as _io
    import json as _json

    class FakeResp:
        def __enter__(self): return _io.BytesIO(_json.dumps(payload).encode())
        def __exit__(self, *a): return False

    monkeypatch.setattr(llm._urlreq, "urlopen", lambda req, timeout=10: FakeResp())
    llm._FREE_CACHE.update(ts=0.0, ids=None)
    ids = llm.free_model_ids(force=True)
    assert ids == {"a/free"}


def test_free_model_ids_network_error_returns_none(monkeypatch):
    def boom(req, timeout=10):
        raise OSError("down")
    monkeypatch.setattr(llm._urlreq, "urlopen", boom)
    llm._FREE_CACHE.update(ts=0.0, ids=None)
    assert llm.free_model_ids(force=True) is None
