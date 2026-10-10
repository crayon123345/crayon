from __future__ import annotations
from dataclasses import dataclass
import logging
from typing import Any
import httpx

log = logging.getLogger(__name__)

@dataclass
class ModelReply:
    text: str
    provider: str
    model: str
    tool_calls: list[dict[str, Any]] | None = None

class ModelRouter:
    def __init__(self, settings, client: httpx.Client | None = None):
        self.s = settings
        self.client = client or httpx.Client(timeout=45)
        self._openrouter_index = 0

    def answer(self, messages: list[dict[str, str]], tools: list[dict[str, Any]] | None = None) -> ModelReply:
        errors = []
        try:
            return self._ollama(messages, tools)
        except Exception as exc:
            errors.append(f"ollama:{type(exc).__name__}")
            log.warning("Ollama unavailable; trying fallback")
        if self.s.openrouter_api_key:
            for _ in range(max(1, len(self.s.openrouter_models))):
                model = self.s.openrouter_models[self._openrouter_index % len(self.s.openrouter_models)] if self.s.openrouter_models else ""
                self._openrouter_index += 1
                try:
                    return self._openrouter(messages, model, tools)
                except Exception as exc:
                    errors.append(f"openrouter:{type(exc).__name__}")
        if self.s.gemini_api_key:
            try:
                return self._gemini(messages)
            except Exception as exc:
                errors.append(f"gemini:{type(exc).__name__}")
        raise RuntimeError("No model backend succeeded: " + ", ".join(errors))

    def _ollama(self, messages, tools):
        payload = {"model": self.s.ollama_model, "messages": messages, "stream": False}
        if tools: payload["tools"] = tools
        r = self.client.post(self.s.ollama_base_url.rstrip("/") + "/v1/chat/completions", json=payload)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        return ModelReply(msg.get("content", "") or "", "ollama", self.s.ollama_model, msg.get("tool_calls") or [])

    def _openrouter(self, messages, model, tools):
        if not model.endswith(":free"): raise ValueError("OpenRouter model must be a :free model")
        payload = {"model": model, "messages": messages}
        if tools: payload["tools"] = tools
        r = self.client.post("https://openrouter.ai/api/v1/chat/completions", headers={"Authorization": f"Bearer {self.s.openrouter_api_key}"}, json=payload)
        if r.status_code == 429: raise RuntimeError("OpenRouter rate limited")
        r.raise_for_status()
        msg = r.json()["choices"][0]["message"]
        return ModelReply(msg.get("content", "") or "", "openrouter", model, msg.get("tool_calls") or [])

    def _gemini(self, messages):
        prompt = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.s.gemini_model}:generateContent"
        r = self.client.post(url, params={"key": self.s.gemini_api_key}, json={"contents": [{"parts": [{"text": prompt}]}]})
        r.raise_for_status()
        text = r.json()["candidates"][0]["content"]["parts"][0]["text"]
        return ModelReply(text, "gemini", self.s.gemini_model)
