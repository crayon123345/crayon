from contextlib import nullcontext
import pytest
import httpx
import cr_web as W

def test_private_host_rejected(monkeypatch):
    monkeypatch.setattr(W,'_safe_host',lambda h:False)
    with pytest.raises(ValueError):W.fetch('http://127.0.0.1')

def test_credentials_rejected(monkeypatch):
    monkeypatch.setattr(W,'_safe_host',lambda h:True)
    with pytest.raises(ValueError):W.fetch('https://user:password@example.com')

def test_stream_final_url_and_html(monkeypatch):
    responses={'https://a.example/':httpx.Response(302,headers={'location':'https://b.example/article'}),
      'https://b.example/article':httpx.Response(200,headers={'content-type':'text/html'},text='<title>Title</title><script>secret</script><article><p>'+('Hello &amp; world. '*20)+'</p><a href="/next">Next</a></article>')}
    class Client:
        def stream(self,m,u):return nullcontext(responses[u])
    monkeypatch.setattr(W,'_c',Client());monkeypatch.setattr(W,'_safe_host',lambda h:True)
    r=W.fetch('https://a.example/')
    assert r['url']=='https://b.example/article' and 'secret' not in r['text'] and '&' in r['text']
    assert r['links']==['https://b.example/next']

def test_redirect_private_blocked(monkeypatch):
    class Client:
        def stream(self,m,u):return nullcontext(httpx.Response(302,headers={'location':'http://127.0.0.1/'}))
    monkeypatch.setattr(W,'_c',Client());monkeypatch.setattr(W,'_safe_host',lambda h:h!='127.0.0.1')
    with pytest.raises(ValueError):W.fetch('https://a.example/')

def test_research_failed_sources_explicit(monkeypatch):
    monkeypatch.setattr(W,'search',lambda *a:[{'url':'https://bad.example'}])
    monkeypatch.setattr(W,'fetch',lambda *a:(_ for _ in ()).throw(RuntimeError('blocked')))
    r=W.research('x');assert not r['pages'] and r['failures'][0]['error']=='blocked'

def test_comparison_queries_keep_context(monkeypatch):
    import cr_web as W
    calls=[]
    monkeypatch.setattr(W,'search',lambda q,n:(calls.append(q) or []))
    W.research('Go deep on a comparison of Notion and Obsidian for student notes. Use current official sources for both.')
    assert calls==['Notion student notes official documentation','Obsidian student notes official documentation']
