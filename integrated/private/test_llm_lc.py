import pytest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

import cr_config as C
import cr_lc
import cr_llm


def test_to_messages_roundtrip_tools():
    contents = [
        {"role": "user", "parts": [{"text": "what time is it"}]},
        {"role": "model", "parts": [{"functionCall": {"name": "get_time", "args": {}}}]},
        {"role": "user", "parts": [{"functionResponse": {"name": "get_time", "response": {"result": {"now": "x"}}}}]},
    ]
    m = cr_lc.to_messages(contents, system="sys")
    assert isinstance(m[0], SystemMessage) and isinstance(m[1], HumanMessage)
    assert m[2].tool_calls[0]["name"] == "get_time"
    assert isinstance(m[3], ToolMessage) and m[3].tool_call_id == m[2].tool_calls[0]["id"]


def test_inline_image_becomes_image_block():
    m = cr_lc.to_messages([{"role": "user", "parts": [{"text": "hi"}, {"inlineData": {"mimeType": "image/png", "data": "AAA"}}]}])
    assert m[0].content[1]["image_url"]["url"] == "data:image/png;base64,AAA"


def test_tool_defs_and_from_ai():
    d = cr_lc.tool_defs([{"functionDeclarations": [{"name": "a", "description": "d", "parameters": {"type": "object", "properties": {}}}]}])
    assert d[0]["function"]["name"] == "a"
    text, calls, parts = cr_lc.from_ai(AIMessage(content="ok", tool_calls=[{"name": "a", "args": {"x": 1}, "id": "1", "type": "tool_call"}]))
    assert text == "ok" and calls == [{"name": "a", "args": {"x": 1}}]
    assert parts[1]["functionCall"]["args"] == {"x": 1}


class FakeChat:
    def __init__(self, reply): self.reply = reply
    def invoke(self, msgs): return AIMessage(content=self.reply)


def test_generate_gemini_via_langchain(monkeypatch):
    monkeypatch.setattr(C, "LLM_FRAMEWORK", "langchain"); monkeypatch.setattr(C, "LLM_PROVIDER", "gemini")
    monkeypatch.setattr(C, "GEMINI_KEY", "k")
    monkeypatch.setattr(cr_lc, "build_model", lambda *a, **k: FakeChat("hello"))
    out = cr_llm.generate([cr_llm.user("hi")])
    assert out["text"] == "hello" and out["raw"]["provider"] == "gemini"


def test_generate_openrouter_uses_openrouter_models(monkeypatch):
    seen = []
    monkeypatch.setattr(C, "LLM_FRAMEWORK", "langchain"); monkeypatch.setattr(C, "LLM_PROVIDER", "openrouter")
    monkeypatch.setattr(C, "OPENROUTER_KEY", "k"); monkeypatch.setattr(C, "OPENROUTER_MODEL", "a/b")
    monkeypatch.setattr(cr_lc, "build_model", lambda model, *a, **k: (seen.append(model), FakeChat("yo"))[1])
    out = cr_llm.generate([cr_llm.user("hi")])
    assert out["text"] == "yo" and seen == ["a/b"] and out["model"] == "a/b"


def test_openrouter_without_key_is_auth_error(monkeypatch):
    monkeypatch.setattr(C, "LLM_PROVIDER", "openrouter"); monkeypatch.setattr(C, "OPENROUTER_KEY", "")
    with pytest.raises(cr_llm.LLMError) as e:
        cr_llm.generate([cr_llm.user("hi")])
    assert e.value.kind == "auth"


def test_quota_error_classified(monkeypatch):
    class Boom:
        def invoke(self, m): raise RuntimeError("429 quota exceeded")
    monkeypatch.setattr(C, "LLM_PROVIDER", "gemini"); monkeypatch.setattr(C, "GEMINI_KEY", "k")
    monkeypatch.setattr(C, "LLM_FRAMEWORK", "langchain")
    monkeypatch.setattr(cr_lc, "build_model", lambda *a, **k: Boom())
    monkeypatch.setattr(cr_llm.time, "sleep", lambda s: None)
    with pytest.raises(cr_llm.LLMError) as e:
        cr_llm.generate([cr_llm.user("hi")], models=["m1"])
    assert e.value.kind == "quota"


def test_build_model_constructs_both(monkeypatch):
    monkeypatch.setattr(C, "GEMINI_KEY", "k"); monkeypatch.setattr(C, "OPENROUTER_KEY", "k")
    monkeypatch.setattr(C, "LLM_PROVIDER", "gemini")
    assert cr_lc.build_model("gemini-2.5-flash", 0.2, 100, True, 0, None) is not None
    monkeypatch.setattr(C, "LLM_PROVIDER", "openrouter")
    assert cr_lc.build_model("openrouter/free", 0.2, 100, True, None, [{"functionDeclarations": [{"name": "a", "description": "d"}]}]) is not None

@pytest.mark.parametrize('mime,kind', [('audio/ogg','audio'),('video/mp4','video'),('application/pdf','file')])
def test_non_image_inline_media_is_preserved(mime,kind):
    m=cr_lc.to_messages([{'role':'user','parts':[{'inlineData':{'mimeType':mime,'data':'eA=='}}]}])
    assert m[0].content[0]=={'type':kind,'mime_type':mime,'base64':'eA=='}


def test_uploaded_file_preserved_and_foreign_provider_blocked(monkeypatch):
    monkeypatch.setattr(C,'LLM_PROVIDER','gemini')
    contents=[{'role':'user','parts':[{'fileData':{'mimeType':'video/mp4','fileUri':'https://generativelanguage.googleapis.com/v1beta/files/test'}}]}]
    m=cr_lc.to_messages(contents)
    assert m[0].content[0]['file_id'].endswith('/files/test')
    monkeypatch.setattr(C,'LLM_PROVIDER','openrouter')
    with pytest.raises(ValueError,match='another provider'):cr_lc.to_messages(contents)


def test_signature_roundtrip_and_reasoning_not_output():
    ai=AIMessage(content=[{'type':'reasoning','text':'private thinking'},{'type':'text','text':'answer'}],tool_calls=[{'name':'a','args':{},'id':'x','type':'tool_call'}],additional_kwargs={'__gemini_function_call_thought_signatures__':{'x':'c2ln'}})
    text,calls,parts=cr_lc.from_ai(ai)
    assert text=='answer' and parts[1]['thoughtSignature']=='c2ln'
    msgs=cr_lc.to_messages([{'role':'model','parts':parts}])
    assert msgs[0].additional_kwargs['__gemini_function_call_thought_signatures__']['call_1']=='c2ln'


def test_direct_path_and_missing_framework_fallback(monkeypatch):
    monkeypatch.setattr(C,'LLM_PROVIDER','gemini');monkeypatch.setattr(C,'LLM_FRAMEWORK','langchain');monkeypatch.setattr(C,'GEMINI_KEY','k')
    monkeypatch.setattr(cr_lc,'to_messages',lambda *a,**k:(_ for _ in ()).throw(cr_lc.Unavailable('missing package')))
    class Response:
        status_code=200
        def json(self):return {'candidates':[{'content':{'parts':[{'text':'direct ok'}]}}]}
    monkeypatch.setattr(cr_llm,'_post',lambda *a:Response())
    assert cr_llm.generate([cr_llm.user('hi')])['text']=='direct ok'
    monkeypatch.setattr(C,'LLM_FRAMEWORK','direct')
    assert cr_llm.generate([cr_llm.user('hi')])['text']=='direct ok'
