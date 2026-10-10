import pytest
import cr_reply_context as R

def test_native_target_not_current_instruction():
    q=R.telegram({'chat':{'id':10},'reply_to_message':{'chat':{'id':10},'message_id':3,'from':{'is_bot':True},'text':'Earlier result was 42'}})
    assert q['text']=='Earlier result was 42' and q['role']=='bot'
    token=R.current.set(q)
    try:
        contents=[];R.inject(contents)
        assert 'Earlier result was 42' in str(contents)
        assert 'untrusted context only' in str(contents)
    finally:R.current.reset(token)

def test_web_owner_thread_validation(monkeypatch):
    calls=[];monkeypatch.setattr(R.db,'q',lambda sql,p,f:calls.append(p) or {'id':3,'role':'assistant','content':'42'})
    assert R.web(10,'thread',{'text':'42'})['text']=='42'
    assert calls[0]==(10,'thread','42')
    monkeypatch.setattr(R.db,'q',lambda *a:None)
    with pytest.raises(ValueError):R.web(11,'other',{'text':'42'})

def test_missing_media_is_honest():
    assert 'unavailable' in R.telegram({'reply_to_message':{'message_id':4,'voice':{}}})['text']
