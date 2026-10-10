import pytest
import cr_web_rooms as R

def test_room_membership_required(monkeypatch):
    monkeypatch.setattr(R.db,'q',lambda *a,**kw:None)
    with pytest.raises(ValueError,match='not a member'):R.allowed(12,'a'*43)
    for room in ('x','../room',True):
        with pytest.raises(ValueError):R.allowed(12,room)

def test_room_explicit_audience(monkeypatch):
    monkeypatch.setattr(R.db,'q',lambda *a,**kw:pytest.fail('No DB before consent'))
    with pytest.raises(ValueError):R.create(12,'Shared',False)
    with pytest.raises(ValueError):R.join(12,'a'*43,False)
    assert 'future members' in R.AUDIENCE and 'No personal memory' in R.AUDIENCE

def test_room_private_context_never_imported(monkeypatch):
    import cr_group as G,cr_web_app as W,contextlib
    calls=[]
    monkeypatch.setattr(R,'allowed',lambda uid,room:{'owner_id':uid})
    monkeypatch.setattr(R.db,'q',lambda sql,p=(),*a,**kw:calls.append((sql,p)) or ({'n':0} if sql.startswith('SELECT count') else None))
    monkeypatch.setattr(R.db,'_conn',lambda:type('C',(),{'transaction':lambda self:contextlib.nullcontext()})())
    seen=[];monkeypatch.setattr(G,'answer',lambda msg:seen.append(msg) or 'Public answer')
    monkeypatch.setattr(W,'encode',lambda v:'encrypted')
    assert R.say(12,'a'*43,'Public question')['text']=='Public answer'
    assert seen==[{'text':'@crayon_v1_bot Public question'}]
    assert not any('messages WHERE user_id' in sql or 'facts' in sql for sql,p in calls)
    with pytest.raises(ValueError):R.say(12,'a'*43,'password: veryprivate123')

def test_room_delete_requires_owner_and_exact_accept(monkeypatch):
    monkeypatch.setattr(R,'allowed',lambda *a:{'owner_id':12})
    for uid,accept in ((13,True),(12,False),(12,'true')):
        with pytest.raises(ValueError):R.delete(uid,'a'*43,accept)
