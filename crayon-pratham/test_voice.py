import pytest
import cr_voice as V

def test_voice_off_no_network(monkeypatch):
    monkeypatch.setattr(V,'enabled',lambda uid:False)
    with pytest.raises(ValueError,match='off'):V.synthesize(1,'hello')

def test_voice_secret_no_network(monkeypatch):
    monkeypatch.setattr(V,'enabled',lambda uid:True)
    monkeypatch.setenv('ELEVENLABS_API_KEY','private');monkeypatch.setenv('ELEVENLABS_VOICE_ID','a'*20)
    with pytest.raises(ValueError):V.synthesize(1,'x'*1201)

def test_paid_plan_hard_stop(monkeypatch):
    monkeypatch.setattr(V,'enabled',lambda uid:True)
    monkeypatch.setenv('ELEVENLABS_API_KEY','private');monkeypatch.setenv('ELEVENLABS_VOICE_ID','a'*20)
    class R:
        def raise_for_status(self):pass
        def json(self):return {'tier':'starter'}
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,*a,**kw):return R()
        def post(self,*a,**kw):raise AssertionError('paid generation')
    monkeypatch.setattr(V.httpx,'Client',Client)
    with pytest.raises(ValueError,match='Free-only'):V.synthesize(1,'hello')

def test_no_automatic_reply_path():
    from pathlib import Path
    assert not hasattr(V,'reply_audio')
    assert 'reply_audio' not in Path('cr_telegram.py').read_text()
    assert 'reply_audio' not in Path('cr_web_app.py').read_text()
