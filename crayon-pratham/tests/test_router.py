import httpx
from core.model_router import ModelRouter
from config.settings import Settings

class MockTransport(httpx.BaseTransport):
    def handle_request(self, request):
        if "11434" in str(request.url): return httpx.Response(503, request=request)
        if "openrouter" in str(request.url): return httpx.Response(429, request=request)
        return httpx.Response(200, request=request, json={"candidates":[{"content":{"parts":[{"text":"gemini fallback"}]}}]})

def test_fallback_to_gemini_after_ollama_and_openrouter():
    s = Settings(openrouter_api_key="or", gemini_api_key="gm", openrouter_models=("x:free",))
    r = ModelRouter(s, httpx.Client(transport=MockTransport()))
    out = r.answer([{"role":"user", "content":"hi"}])
    assert out.provider == "gemini" and out.text == "gemini fallback"
