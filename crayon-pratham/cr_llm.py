"""LLM entry point. LangChain layer (cr_lc) by default with Gemini or OpenRouter; direct Gemini REST path kept as fallback."""
import json
import logging
import time

import httpx

import cr_config as C
from cr_safety import redact

log = logging.getLogger("crayon.llm")
_client = httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0))
_BASE = "https://generativelanguage.googleapis.com/v1beta/models/"


class LLMError(Exception):
    def __init__(self, kind, detail=""):
        super().__init__(f"{kind}: {detail}")
        self.kind = kind  # quota | auth | unavailable | bad_request | empty
        self.detail = detail


def _post(model, body):
    r = _client.post(_BASE + model + ":generateContent", headers={"x-goog-api-key": C.GEMINI_KEY}, json=body)
    return r


def generate(contents, system="", tools=None, json_mode=False, temperature=0.6, max_tokens=1500,
             models=None, thinking_budget=None):
    try:
        return _generate_primary(contents,system,tools,json_mode,temperature,max_tokens,models,thinking_budget)
    except LLMError as error:
        import cr_lc
        if error.kind not in ('quota','unavailable','auth') or cr_lc.provider()!='gemini' or not C.OPENROUTER_AUTO_FALLBACK or not C.OPENROUTER_KEY:raise
        # Gemini uploads cannot be forwarded, and non-image media lacks accepted OR proof.
        for content in contents:
            for part in content.get('parts',[]):
                if 'fileData' in part or ('inlineData' in part and not part['inlineData'].get('mimeType','').startswith('image/')):raise error
        return openrouter_fallback(contents,system,tools,json_mode,temperature,max_tokens)


def openrouter_fallback(contents,system='',tools=None,json_mode=False,temperature=0.6,max_tokens=1500):
    import cr_lc
    if not C.OPENROUTER_KEY:raise LLMError('auth','OpenRouter fallback key absent')
    # Fixed free router and explicit zero-price/provider-data guards; never use configured paid defaults.
    chat=cr_lc.build_model('openrouter/free',temperature,max_tokens,json_mode,None,tools,selected_provider='openrouter')
    try:ai=chat.invoke(cr_lc.to_messages(contents,system,selected_provider='openrouter'))
    except Exception as e:raise LLMError(cr_lc.classify(e),'Free OpenRouter fallback unavailable; no paid route attempted') from None
    text,calls,parts=cr_lc.from_ai(ai)
    if not text and not calls:raise LLMError('empty','Free fallback returned no content; finish='+str(getattr(ai,'response_metadata',{}).get('finish_reason','unknown')))
    return {'text':text,'calls':calls,'parts':parts,'model':'openrouter/free','raw':{'provider':'openrouter','fallback':True,'actual_model':getattr(ai,'response_metadata',{}).get('model_name','unknown')}}


def _generate_primary(contents, system="", tools=None, json_mode=False, temperature=0.6, max_tokens=1500,
             models=None, thinking_budget=None):
    """Returns dict: {text, calls:[{name,args}], raw_parts, model}. Raises LLMError."""
    import cr_lc
    if cr_lc.enabled() and cr_lc.has_key():
        try:
            return _generate_lc(contents, system, tools, json_mode, temperature, max_tokens, models, thinking_budget)
        except cr_lc.Unavailable as e:
            log.warning("langchain unavailable, using direct path: %s", redact(str(e))[:150])
    if cr_lc.provider() == "openrouter":
        raise LLMError("auth", "no OPENROUTER_API_KEY or langchain missing")
    if not C.GEMINI_KEY:
        raise LLMError("auth", "no GEMINI_API_KEY")
    body = {"contents": contents,
            "generationConfig": {"temperature": temperature, "maxOutputTokens": max_tokens}}
    if system:
        body["systemInstruction"] = {"parts": [{"text": system}]}
    if tools:
        body["tools"] = tools
    if json_mode:
        body["generationConfig"]["responseMimeType"] = "application/json"
    errs = []
    for model in (models or [C.GEMINI_MODEL] + C.GEMINI_FALLBACKS):
        for attempt in range(2):  # 429: wait and retry once, then next model
            try:
                b = body
                if thinking_budget is not None and "2.5" in model:  # newer models reject thinkingBudget
                    b = {**body, "generationConfig": {**body["generationConfig"], "thinkingConfig": {"thinkingBudget": thinking_budget}}}
                r = _post(model, b)
            except httpx.HTTPError as e:
                errs.append(f"{model}:net:{type(e).__name__}")
                time.sleep(1.0)
                continue
            if r.status_code == 200:
                data = r.json()
                cands = data.get("candidates") or []
                if not cands or not cands[0].get("content", {}).get("parts"):
                    reason = (cands[0].get("finishReason") if cands else (data.get("promptFeedback") or {}).get("blockReason"))
                    errs.append(f"{model}:empty:{reason}")
                    break
                parts = cands[0]["content"]["parts"]
                text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
                calls = [{"name": p["functionCall"]["name"], "args": p["functionCall"].get("args", {}) or {}}
                         for p in parts if "functionCall" in p]
                return {"text": text.strip(), "calls": calls, "parts": parts, "model": model, "raw": data}
            if r.status_code == 429:
                errs.append(f"{model}:429")
                time.sleep(4.0 if attempt == 0 else 0)
                continue
            if r.status_code in (401, 403):
                raise LLMError("auth", f"HTTP {r.status_code}")
            if r.status_code == 404:
                errs.append(f"{model}:404")
                break
            if r.status_code == 400:
                raise LLMError("bad_request", redact(r.text[:300]))
            errs.append(f"{model}:{r.status_code}")
            time.sleep(1.0)
    kind = "quota" if any(e.endswith(":429") for e in errs) else "unavailable"
    raise LLMError(kind, ", ".join(errs))


def _generate_lc(contents, system, tools, json_mode, temperature, max_tokens, models, thinking_budget):
    import cr_lc
    msgs = cr_lc.to_messages(contents, system)
    errs = []
    for model in cr_lc.model_names(models):
        for attempt in range(2):
            try:
                chat = cr_lc.build_model(model, temperature, max_tokens, json_mode, thinking_budget, tools)
                import cr_stream
                ai = cr_stream.consume(chat,msgs) if cr_stream.allowed.get() and cr_stream.callback.get() and not json_mode else chat.invoke(msgs)
            except cr_lc.Unavailable:
                raise
            except Exception as e:
                kind = cr_lc.classify(e)
                errs.append(f"{model}:{kind}")
                if kind == "auth":
                    raise LLMError("auth", redact(str(e))[:200])
                if kind == "bad_request":
                    raise LLMError("bad_request", redact(str(e))[:300])
                if kind == "quota" and attempt == 0:
                    time.sleep(4.0)
                    continue
                if kind == "notfound":
                    break
                time.sleep(1.0)
                continue
            text, calls, parts = cr_lc.from_ai(ai)
            if not text and not calls:
                errs.append(f"{model}:empty")
                break
            return {"text": text, "calls": calls, "parts": parts, "model": model, "raw": {"provider": cr_lc.provider()}}
    kind = "quota" if any(e.endswith(":quota") for e in errs) else "unavailable"
    raise LLMError(kind, ", ".join(errs))


def user(text):
    return {"role": "user", "parts": [{"text": text}]}


def model_msg(text):
    return {"role": "model", "parts": [{"text": text}]}


def ask_json(prompt, system="", default=None, temperature=0.2, max_tokens=800):
    """One-shot structured call. Returns parsed JSON or default on any failure."""
    try:
        out = generate([user(prompt)], system=system, json_mode=True, temperature=temperature,
                       max_tokens=max_tokens, thinking_budget=0)
        t = out["text"].strip()
        if t.startswith("```"):
            t = t.strip("`")
            t = t[t.find("\n") + 1:] if "\n" in t else t
        return json.loads(t)
    except Exception as e:
        log.warning("ask_json failed: %s", redact(str(e))[:200])
        return default
