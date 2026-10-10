import pytest
import cr_google as G

def test_signature_ignores_profile_and_extracted_fact(monkeypatch):
    monkeypatch.setattr(G.db,'kv_get',lambda *a:None)
    monkeypatch.setattr(G.db,'q',lambda *a,**kw:pytest.fail('profile/fact used'))
    assert G.sender_name(10)==''

def test_signature_setting_encrypted_and_per_member(monkeypatch):
    store={}
    monkeypatch.setattr(G.db,'kv_get',lambda k,d=None:store.get(k,d))
    monkeypatch.setattr(G.db,'kv_set',lambda k,v:store.__setitem__(k,v))
    monkeypatch.setattr(G,'encrypt',lambda uid,c:{'uid':uid,'name':c['name']})
    monkeypatch.setattr(G,'decrypt',lambda uid,c:c if c['uid']==uid else {})
    G.set_sender_name(10,'Pratham');G.set_sender_name(20,'Sam')
    assert G.sender_name(10)=='Pratham' and G.sender_name(20)=='Sam' and G.sender_name(30)==''

@pytest.mark.parametrize('name',['','a\nb','<x>','person@example.com','x'*101])
def test_signature_invalid_name_rejected(name):
    assert not G.valid_signature_name(name)

def test_signature_pending_blocks_button_send(monkeypatch):
    monkeypatch.setattr(G.db,'kv_get',lambda *a:{'draft':'pending'})
    monkeypatch.setattr(G.db,'q',lambda *a,**kw:pytest.fail('send claimed'))
    with pytest.raises(G.GoogleError,match='signature'):G.send_draft(10,'old','hash')

def test_owner_explicit_override_saved_not_global(monkeypatch):
    store={}
    monkeypatch.setattr(G.db,'kv_get',lambda k,d=None:store.get(k,d))
    monkeypatch.setattr(G.db,'kv_set',lambda k,v:store.__setitem__(k,v))
    monkeypatch.setattr(G,'encrypt',lambda uid,c:c)
    monkeypatch.setattr(G,'decrypt',lambda uid,c:c)
    assert G.sender_name(1898030949)=='Pratham'
    assert store['google_signature_name_1898030949']=={'name':'Pratham'}
    assert G.sender_name(22)==''
