import pytest
import cr_workspace_create as C

def test_limits():
    assert C.fields('doc','Title','Text')['content']=='Text'
    with pytest.raises(ValueError):C.fields('slides','Title',[{'title':'a','body':'x'*1001}])
    with pytest.raises(ValueError):C.fields('sheet','Title',[[float('nan')]])

def test_account_bound_doc_create(monkeypatch):
    monkeypatch.setattr(C.X,'status',lambda *a:{'identity':'member@example.com'})
    calls=[]
    def call(uid,m,u,b=None,params=None):
        calls.append((uid,m,u,b));return {'documentId':'d'*20} if u.endswith('/documents') else {}
    monkeypatch.setattr(C.W,'_call',call)
    monkeypatch.setattr(C.W,'doc_read',lambda *a:{'text':'Hello'})
    r=C.apply(20,{'kind':'doc','title':'Title','content':'Hello','account':'member@example.com'})
    assert r['created'] and r['content_readback_verified'] and not r['visual_verified']
    assert all(x[0]==20 for x in calls) and len(calls)==3

def test_changed_account_blocks_before_create(monkeypatch):
    monkeypatch.setattr(C.X,'status',lambda *a:{'identity':'other@example.com'})
    monkeypatch.setattr(C.W,'_call',lambda *a:pytest.fail('write'))
    with pytest.raises(ValueError):C.apply(20,{'kind':'doc','title':'Title','content':'Hello','account':'member@example.com'})
