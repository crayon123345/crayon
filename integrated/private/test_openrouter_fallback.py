import pytest
import cr_llm as L
import cr_lc as LC
import cr_config as C
from langchain_core.messages import AIMessage
@pytest.fixture(autouse=True)
def fallback_enabled(monkeypatch):
    monkeypatch.setattr(C,'LLM_PROVIDER','gemini');monkeypatch.setattr(C,'OPENROUTER_AUTO_FALLBACK',True);monkeypatch.setattr(C,'OPENROUTER_KEY','synthetic')
def test_primary_success_never_falls_back(monkeypatch):
    monkeypatch.setattr(L,'_generate_primary',lambda *a:{'text':'primary'})
    monkeypatch.setattr(L,'openrouter_fallback',lambda *a:pytest.fail('fallback not needed'))
    assert L.generate([L.user('test')])['text']=='primary'
def test_forced_quota_uses_free_route(monkeypatch):
    def fail(*a):raise L.LLMError('quota','synthetic')
    monkeypatch.setattr(L,'_generate_primary',fail);calls=[]
    class Chat:
        def invoke(self,msgs):return AIMessage(content='FALLBACK_READY')
    monkeypatch.setattr(LC,'build_model',lambda model,*a,**kw:calls.append((model,kw)) or Chat())
    r=L.generate([L.user('test')]);assert r['text']=='FALLBACK_READY' and r['raw']['fallback']
    assert calls==[('openrouter/free',{'selected_provider':'openrouter'})]
def test_invalid_request_does_not_fallback(monkeypatch):
    def fail(*a):raise L.LLMError('bad_request')
    monkeypatch.setattr(L,'_generate_primary',fail);monkeypatch.setattr(L,'openrouter_fallback',lambda *a:pytest.fail('bad request'))
    with pytest.raises(L.LLMError):L.generate([L.user('test')])
def test_media_uri_and_audio_never_forwarded(monkeypatch):
    def fail(*a):raise L.LLMError('quota')
    monkeypatch.setattr(L,'_generate_primary',fail);monkeypatch.setattr(L,'openrouter_fallback',lambda *a:pytest.fail('media must stay Gemini'))
    for part in ({'fileData':{'fileUri':'private','mimeType':'video/mp4'}},{'inlineData':{'mimeType':'audio/wav','data':'AAA'}}):
        with pytest.raises(L.LLMError):L.generate([{'role':'user','parts':[part]}])
def test_free_zero_price_private_provider_guard(monkeypatch):
    import langchain_openai
    calls=[]
    monkeypatch.setattr(langchain_openai,'ChatOpenAI',lambda **kw:calls.append(kw) or object())
    LC.build_model('openrouter/free',0,20,False,None,None,selected_provider='openrouter')
    p=calls[0]['extra_body']['provider'];assert set(p['max_price'].values())=={0} and p['data_collection']=='deny'
    with pytest.raises(ValueError,match='Paid'):LC.build_model('paid/model',0,20,False,None,None,selected_provider='openrouter')
def test_disabled_or_missing_key_preserves_error(monkeypatch):
    def fail(*a):raise L.LLMError('quota')
    monkeypatch.setattr(L,'_generate_primary',fail);monkeypatch.setattr(L,'openrouter_fallback',lambda *a:pytest.fail('disabled'))
    monkeypatch.setattr(C,'OPENROUTER_AUTO_FALLBACK',False)
    with pytest.raises(L.LLMError):L.generate([L.user('test')])
