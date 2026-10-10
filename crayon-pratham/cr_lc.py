"""LangChain layer. Same input/output shape as cr_llm.generate (Gemini-style contents,
tools and parts), so callers do not change. Providers: gemini (default), openrouter."""
import json
import logging

import cr_config as C

log = logging.getLogger("crayon.lc")
OPENROUTER_BASE = "https://openrouter.ai/api/v1"


class Unavailable(Exception):
    """LangChain or the provider key is not available; caller should use the direct path."""


def provider():
    p = C.LLM_PROVIDER
    return p if p in ("gemini", "openrouter") else "gemini"


def enabled():
    return C.LLM_FRAMEWORK == "langchain"


def has_key(p=None):
    return bool(C.OPENROUTER_KEY if (p or provider()) == "openrouter" else C.GEMINI_KEY)


def model_names(models=None):
    if provider() == "openrouter":
        return [C.OPENROUTER_MODEL] + C.OPENROUTER_FALLBACKS
    return models or [C.GEMINI_MODEL] + C.GEMINI_FALLBACKS


def _schema(node):
    """Gemini schema (lowercase types) -> plain JSON schema."""
    if isinstance(node, dict):
        return {k: _schema(v) for k, v in node.items()}
    if isinstance(node, list):
        return [_schema(v) for v in node]
    return node


def tool_defs(tools):
    out = []
    for group in tools or []:
        for d in group.get("functionDeclarations", []):
            out.append({"type": "function", "function": {
                "name": d["name"], "description": d.get("description", ""),
                "parameters": _schema(d.get("parameters") or {"type": "object", "properties": {}})}})
    return out


def to_messages(contents, system="", selected_provider=None):
    try:
        from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage
    except ImportError as e:
        raise Unavailable(str(e)) from e
    msgs = [SystemMessage(content=system)] if system else []
    pending = {}  # function name -> queue of tool_call ids
    n = 0
    for c in contents:
        parts = c.get("parts") or []
        if c.get("role") == "model":
            text = "".join(p.get("text", "") for p in parts if "text" in p and not p.get("thought"))
            calls, signatures = [], {}
            for p in parts:
                if "functionCall" in p:
                    n += 1
                    fc = p["functionCall"]
                    cid = f"call_{n}"
                    if p.get("thoughtSignature"):
                        signatures[cid] = p["thoughtSignature"]
                    pending.setdefault(fc["name"], []).append(cid)
                    calls.append({"name": fc["name"], "args": fc.get("args") or {}, "id": cid, "type": "tool_call"})
            msgs.append(AIMessage(content=text, tool_calls=calls, additional_kwargs={"__gemini_function_call_thought_signatures__": signatures} if signatures else {}))
            continue
        blocks, tool_msgs = [], []
        for p in parts:
            if "text" in p:
                blocks.append({"type": "text", "text": p["text"]})
            elif "inlineData" in p:
                d = p["inlineData"]
                mime = d["mimeType"]
                if mime.startswith("image/"):
                    blocks.append({"type": "image_url", "image_url": {"url": f"data:{mime};base64,{d['data']}"}})
                else:
                    kind = "audio" if mime.startswith("audio/") else "video" if mime.startswith("video/") else "file"
                    blocks.append({"type": kind, "base64": d["data"], "mime_type": mime})
            elif "fileData" in p:
                d = p["fileData"]
                if (selected_provider or provider()) != "gemini":
                    raise ValueError("Gemini uploaded files cannot be sent to another provider")
                blocks.append({"type": "file", "file_id": d["fileUri"], "mime_type": d["mimeType"]})
            elif "functionResponse" in p:
                fr = p["functionResponse"]
                q = pending.get(fr["name"]) or []
                cid = q.pop(0) if q else f"call_{fr['name']}"
                tool_msgs.append(ToolMessage(content=json.dumps(fr.get("response"), default=str), tool_call_id=cid, name=fr["name"]))
        msgs.extend(tool_msgs)
        if blocks:
            only_text = all(b["type"] == "text" for b in blocks)
            msgs.append(HumanMessage(content="\n".join(b["text"] for b in blocks) if only_text else blocks))
    return msgs


def build_model(model, temperature, max_tokens, json_mode, thinking_budget, tools, selected_provider=None):
    p = selected_provider or provider()
    try:
        if p == "openrouter":
            if model!='openrouter/free' and not model.endswith(':free'):raise ValueError('Paid OpenRouter models blocked')
            from langchain_openai import ChatOpenAI
            kw = {"response_format": {"type": "json_object"}} if json_mode else {}
            m = ChatOpenAI(model=model, api_key=C.OPENROUTER_KEY, base_url=OPENROUTER_BASE, temperature=temperature,
                           max_tokens=max_tokens, timeout=60, max_retries=0, model_kwargs=kw,
                           default_headers={"X-Title": "Crayon"}, extra_body={"provider":{"max_price":{"prompt":0,"completion":0,"request":0,"image":0},"data_collection":"deny","require_parameters":True}})
        else:
            from langchain_google_genai import ChatGoogleGenerativeAI
            kw = {"response_mime_type": "application/json"} if json_mode else {}
            if thinking_budget is not None and "2.5" in model:
                kw["thinking_budget"] = thinking_budget
            m = ChatGoogleGenerativeAI(model=model, google_api_key=C.GEMINI_KEY, temperature=temperature,
                                       max_output_tokens=max_tokens, timeout=60, max_retries=0, **kw)
    except ImportError as e:
        raise Unavailable(str(e))
    defs = tool_defs(tools)
    return m.bind_tools(defs) if defs else m


def from_ai(msg):
    content = msg.content
    if isinstance(content, list):
        content = "".join(b.get("text", "") if isinstance(b, dict) and b.get("type", "text") == "text" else "" if isinstance(b, dict) else str(b) for b in content)
    text = (content or "").strip()
    calls = [{"name": c["name"], "args": c.get("args") or {}} for c in (getattr(msg, "tool_calls", None) or [])]
    signatures = (getattr(msg, "additional_kwargs", None) or {}).get("__gemini_function_call_thought_signatures__", {})
    parts = [{"text": text}] if text else []
    for call, raw in zip(calls, (getattr(msg, "tool_calls", None) or [])):
        part = {"functionCall": {"name": call["name"], "args": call["args"]}}
        if signatures.get(raw.get("id")):
            part["thoughtSignature"] = signatures[raw["id"]]
        parts.append(part)
    return text, calls, parts


def classify(exc):
    s = f"{type(exc).__name__} {exc}".lower()
    if "429" in s or "quota" in s or "rate" in s and "limit" in s or "resourceexhausted" in s:
        return "quota"
    if "401" in s or "403" in s or "api key" in s or "unauthorized" in s or "permission" in s:
        return "auth"
    if "400" in s or "invalid_argument" in s or "badrequest" in s:
        return "bad_request"
    if "404" in s or "not found" in s:
        return "notfound"
    return "unavailable"
