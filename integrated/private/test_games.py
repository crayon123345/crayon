import cr_games as G
from cr_telegram import CaptureOut

def test_quiz_account_and_chat_isolation(monkeypatch):
    state={}
    monkeypatch.setattr(G.db,'kv_set',lambda k,v:state.update({k:v}))
    monkeypatch.setattr(G.db,'kv_get',lambda k:state.get(k))
    monkeypatch.setattr(G.secrets,'randbelow',lambda n:0)
    out=CaptureOut();assert G.handle(12,12,'/play quiz',out)
    G.handle(13,12,'/answer 2',out);assert 'No active' in out.sent[-1]['text']
    G.handle(12,13,'/answer 2',out);assert 'No active' in out.sent[-1]['text']
    G.handle(12,12,'/answer 2',out);assert 'Correct' in out.sent[-1]['text']
    assert not state['game:12:12']
