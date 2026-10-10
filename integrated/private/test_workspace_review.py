import json,hashlib,pytest
import cr_workspace_review as R
import cr_connections as X

def test_private_preview_owner_binding(monkeypatch):
    calls=[]
    monkeypatch.setattr(R.S,'sheet_preview',lambda uid,*args:{'payload':{'before':[[1]]},'hash':'h'})
    monkeypatch.setattr(X,'status',lambda uid,p:{'identity':str(uid)+'@example.com'})
    monkeypatch.setattr(R.db,'q',lambda sql,p=(),*a,**kw:calls.append((sql,p)))
    monkeypatch.setattr(R.G,'encrypt',lambda uid,c:'encrypted_'+str(uid))
    d=R.preview(12,12,'document123456789','A1',[[2]])
    assert '12@example.com' in d['text'] and 'not written' in d['text']
    assert calls[-1][1][1:3]==(12,12)
    with pytest.raises(ValueError):R.preview(12,13,'document123456789','A1',[[2]])

def test_confirm_account_switch_or_replay_blocks(monkeypatch):
    content={'account':'a@example.com','preview':{'payload':{'before':[[1]]},'hash':'h'}}
    digest=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
    claim=[]
    def q(sql,p=(),*a,**kw):
        if 'RETURNING encrypted' in sql:
            assert p[1:3]==(12,12)
            if claim:return None
            claim.append(True);return {'encrypted':'e','content_hash':digest}
    monkeypatch.setattr(R.db,'q',q);monkeypatch.setattr(R.G,'decrypt',lambda uid,v:content)
    monkeypatch.setattr(X,'status',lambda uid,p:{'identity':'changed@example.com'})
    monkeypatch.setattr(R.S,'sheet_apply',lambda *a:pytest.fail('Changed account must not write'))
    with pytest.raises(ValueError,match='changed'):R.confirm(12,12,'id',digest[:12],'confirm')
    with pytest.raises(ValueError,match='already used'):R.confirm(12,12,'id',digest[:12],'confirm')

def test_exact_once_write_and_cancel(monkeypatch):
    content={'account':'a@example.com','preview':{'payload':{'before':[[1]]},'hash':'h'}}
    digest=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest();writes=[]
    monkeypatch.setattr(R.db,'q',lambda sql,p=(),*a,**kw:{'encrypted':'e','content_hash':digest} if 'RETURNING encrypted' in sql else None)
    monkeypatch.setattr(R.G,'decrypt',lambda uid,v:content);monkeypatch.setattr(X,'status',lambda uid,p:{'identity':'a@example.com'})
    monkeypatch.setattr(R.S,'sheet_apply',lambda uid,p,h:writes.append((uid,p,h)) or {'verified':True})
    assert 'No Sheet change' in R.confirm(12,12,'id',digest[:12],'cancel') and not writes
    assert 'true' in R.confirm(12,12,'id',digest[:12],'confirm') and writes[0][0]==12
