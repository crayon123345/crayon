import pytest
import cr_conversations as C
import cr_memory as M

def test_require_scoped_missing(monkeypatch):
    calls=[]
    monkeypatch.setattr(C.db,'q',lambda *a,**k:calls.append(a) or None)
    with pytest.raises(ValueError):C.require(2,'a'*43)
    assert calls[0][1]==(2,'a'*43)

def test_turn_context_isolated(monkeypatch):
    calls=[]
    monkeypatch.setattr(M.db,'q',lambda *a,**k:calls.append(a) or [])
    token=C.current.set('thread')
    try:
        M.add_message(2,'user','hello');M.recent_messages(2)
    finally:C.current.reset(token)
    assert calls[0][1][-3]=='thread'
    assert calls[1][1][1]=='thread'
    M.recent_messages(2)
    assert 'conversation_id IS NULL' in calls[-1][0]

def test_create_validates_no_guess(monkeypatch):
    with pytest.raises(ValueError):C.create(2,'')

def test_list_dates_json_safe(monkeypatch):
    import json
    from datetime import datetime
    monkeypatch.setattr(C.db,'q',lambda *a,**kw:[{'id':'a'*43,'title':'First','created_at':datetime(2026,10,10)}])
    assert '2026' in json.dumps(C.list_for(2))
