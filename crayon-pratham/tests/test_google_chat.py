import cr_google_chat as H
import cr_telegram as T
import pytest

@pytest.fixture
def store(monkeypatch):
    values={}
    monkeypatch.setattr(H.G,'sender_name',lambda uid:'Test Member')
    monkeypatch.setattr(H.db,'kv_get',lambda k,d=None:values.get(k,d))
    monkeypatch.setattr(H.db,'kv_set',lambda k,v:values.__setitem__(k,v))
    monkeypatch.setattr(H.G,'encrypt',lambda uid,c:c)
    monkeypatch.setattr(H.G,'decrypt',lambda uid,c:c)
    return values

def test_name_not_guessed(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda t:{'action':'draft','recipient_name':'Sam','to':'sam@example.com','subject':'Meeting','body':'Moved to 5.'})
    monkeypatch.setattr(H.G,'make_draft',lambda *a,**k:pytest.fail('guessed address'))
    out=T.CaptureOut();assert H.handle(10,10,'Email Sam saying moved to 5',{},out)
    assert "email address" in out.sent[0]['text']

def test_send_binds_reviewed_pointer(monkeypatch,store):
    monkeypatch.setattr(H.G,'current_draft',lambda uid:('id','hash'))
    monkeypatch.setattr(H.G,'send_draft',lambda *a:pytest.fail('not reviewed'))
    out=T.CaptureOut();H.handle(10,10,'send it',{},out)
    assert 'review' in out.sent[0]['text']

def test_send_same_reviewed(monkeypatch,store):
    store['google_reviewed_10']=['id','hash']
    monkeypatch.setattr(H.G,'current_draft',lambda uid:('id','hash'))
    called=[];monkeypatch.setattr(H.G,'send_draft',lambda *a:called.append(a) or 'Sent')
    H.handle(10,10,'send it',{},T.CaptureOut());assert called==[(10,'id','hash')]

def test_forwarded_rejected(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda *a:pytest.fail('forward parsed'))
    # An actual populated forwarded origin is rejected.
    out=T.CaptureOut();H.handle(10,10,'email Sam',{'forward_origin':{'type':'user'}},out)
    assert 'directly' in out.sent[0]['text']

def test_google_results_never_model(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda t:{'action':'inbox','query':'from:alex'})
    monkeypatch.setattr(H.G,'inbox',lambda *a,**k:'Private inbox result')
    out=T.CaptureOut();H.handle(10,10,'any mail from Alex?',{},out)
    assert out.sent[-1]['text']=='Private inbox result'

def test_friendly_draft_hides_ids(monkeypatch,store):
    monkeypatch.setattr(H.G,'make_draft',lambda *a,**kw:{'text':'Draft only, not sent.\nTo: sam@example.com\nHi','id':'hiddenid','hash':'hiddenhash'})
    out=T.CaptureOut();H.show_draft(10,10,out,'fields')
    assert 'hiddenid' not in out.sent[0]['text'] and out.sent[0]['markup']
    assert store['google_reviewed_10']==['hiddenid','hiddenhash']
