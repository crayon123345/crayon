import json
import pytest
from cryptography.fernet import Fernet
import cr_web_actions as X
import cr_web_auth as A
import cr_web_http as H
from test_web_app import Handler

@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setattr(X.db,'q',lambda *a,**k:None)
    return monkeypatch

def test_web_preview_only_no_send(config):
    calls=[]
    config.setattr(X.G,'make_draft',lambda uid,f,structured,channel:{'id':'draft','hash':'hash','html':'<p>Exact body</p>','fields':f,'text':'From: own@example.com\nTo: user@example.com\nExact body'})
    config.setattr(X.G,'send_draft',lambda *a:pytest.fail('preview cannot send'))
    config.setattr(X.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)))
    r=X.preview(17,'Bearer '+'a'*43,{'kind':'email','fields':dict(to=['user@example.com'],cc=[],bcc=[],subject='Hi',body='Body')})
    saved=next(p for sql,p in calls if sql.startswith('INSERT'))
    assert saved[1:3]==(17,A.digest('a'*43)) and saved[4]==r['hash']
    assert 'Exact body' in r['text'] and 'Exact body' not in saved[3]

def test_confirmation_wrong_session_cannot_send(config):
    calls=[]
    config.setattr(X.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or None)
    config.setattr(X.G,'send_draft',lambda *a:pytest.fail('no send'))
    with pytest.raises(ValueError):X.confirm(17,'Bearer '+'b'*43,{'review_id':'review','hash':'hash','decision':'confirm'})
    claim=next(p for sql,p in calls if sql.startswith('UPDATE'))
    assert claim==('review',17,A.digest('b'*43),'hash')

def test_confirmation_exact_one_use(config):
    data={'kind':'email','payload':{'id':'draft','hash':'short'},'text':'Exact recipients and body'}
    import hashlib
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    row={'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest}
    claims=[];sent=[]
    def q(sql,p=(),fetch='all'):
        if sql.startswith('UPDATE web_action_reviews SET status=') and "'claimed'" in sql:
            claims.append(p);return row if len(claims)==1 else None
    config.setattr(X.db,'q',q);config.setattr(X.G,'send_draft',lambda *a,**kw:sent.append(a) or {'text':'Sent','message_id':'msg-test'})
    body={'review_id':'r','hash':digest,'decision':'confirm'}
    assert X.confirm(17,'Bearer '+'a'*43,body)['text']=='Sent'
    with pytest.raises(ValueError):X.confirm(17,'Bearer '+'a'*43,body)
    assert sent==[(17,'draft','short')]

def test_private_reads_no_model_or_history(config):
    config.setattr(X.G,'inbox',lambda uid,q:'private result')
    r=X.read(17,{'kind':'inbox','fields':{'query':'newer_than:1d'}})
    assert r['text']=='private result' and not r['stored_in_history'] and not r['sent_to_ai']
    with pytest.raises(ValueError):X.read(17,{'kind':'inbox','fields':{'query':'','uid':99}})
    config.setattr(X.G,'calendar',lambda uid:'calendar_'+str(uid))
    assert X.read(17,{'kind':'calendar','fields':{}})['text']=='calendar_17'

def test_actions_require_authenticated_exact_origin(config):
    config.setattr(A,'session',lambda *a:(_ for _ in ()).throw(A.AuthError('Login first')))
    for path in ['/web/action-preview','/web/action-confirm','/web/private-read','/web/connect']:
        h=Handler(path,{'Origin':A.origin(),'Content-Type':'application/json'})
        H.handle(h,'POST',b'{}');assert h.code==401
        h=Handler(path,{'Origin':'https://evil.test','Content-Type':'application/json'})
        H.handle(h,'POST',b'{}');assert h.code==403


def test_google_send_claim_includes_channel(config):
    calls=[]
    config.setattr(X.db,'kv_get',lambda *a:None)
    config.setattr(X.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or None)
    with pytest.raises(X.G.GoogleError):X.G.send_draft(17,'id','hash',channel='web')
    assert calls[0][1]==('id',17,'web','hash') and 'origin=%s' in calls[0][0]


def test_connections_no_workspace_returns_none(config):
    import cr_connections as C
    config.setattr(X.G,'status',lambda uid:{'email':'own@example.com'})
    config.setattr(C,'status',lambda uid,p:None)
    assert X.connections(17)['workspace'] is None


def test_telegram_draft_lookup_excludes_web(config):
    calls=[]
    config.setattr(X.db,'q',lambda sql,p=(),fetch='all':calls.append(sql) or [])
    with pytest.raises(X.G.GoogleError):X.G.current_draft(17)
    assert "origin='telegram'" in calls[0]

def test_forget_preview_exact_fact_only(config):
    import cr_memory as M
    config.setattr(M,'facts',lambda *a:[{'key':'favorite_color','value':'blue'}])
    r=X.preview(17,'Bearer '+'a'*43,{'kind':'forget','fields':{'key':'favorite_color'}})
    assert 'favorite_color: blue' in r['text'] and 'not all-data deletion' in r['text']
    with pytest.raises(ValueError):X.preview(17,'Bearer '+'a'*43,{'kind':'forget','fields':{'key':'Other Key'}})

def test_forget_compare_before_delete(config):
    import hashlib,cr_memory as M,contextlib
    data={'kind':'forget','payload':{'key':'color','value':'blue'},'text':'Exact blue fact'}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    row={'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest}
    calls=[]
    def q(sql,p=(),*a,**kw):
        calls.append((sql,p))
        if sql.startswith('UPDATE web_action_reviews') and "'claimed'" in sql:return row
        if sql.startswith('DELETE FROM facts'):return {'key':'color'}
    config.setattr(X.db,'q',q);config.setattr(M,'user_lock',lambda uid:contextlib.nullcontext())
    assert 'other records were not deleted' in X.confirm(17,'Bearer '+'a'*43,{'review_id':'r','hash':digest,'decision':'confirm'})['text']
    assert next(p for sql,p in calls if sql.startswith('DELETE FROM facts'))==(17,'color','blue')

def test_form_web_preview_keeps_session_ticket(config):
    import cr_booking as B
    calls=[]
    config.setattr(B,'preview',lambda uid,adapter,values:{'id':'form_id','hash':'form_hash','text':'Destination and exact disclosed fields. Controlled demo only.'})
    config.setattr(X.db,'q',lambda sql,p=(),*a,**kw:calls.append((sql,p)))
    result=X.preview(17,'Bearer '+'a'*43,{'kind':'form','fields':{'adapter':'demo','values':{'name':'TEST ONLY'}}})
    saved=next(p for sql,p in calls if sql.startswith('INSERT'))
    assert saved[1:3]==(17,A.digest('a'*43)) and result['kind']=='form'

def test_form_web_confirmation_result_excludes_image(config):
    import hashlib,cr_booking as B
    data={'kind':'form','payload':{'id':'form','hash':'short'},'text':'Reviewed exact form'}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    row={'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest}
    config.setattr(X.db,'q',lambda sql,*a,**kw:row if sql.startswith('UPDATE') and "'claimed'" in sql else None)
    config.setattr(B,'submit',lambda *a:{'ok':True,'verified':True,'note':'Controlled test received','screenshot':'PRIVATE_BASE64'})
    r=X.confirm(17,'Bearer '+'a'*43,{'review_id':'r','hash':digest,'decision':'confirm'})
    assert 'PRIVATE_BASE64' not in r['text'] and 'not proof of a real reservation' in r['text']

def test_compose_preserves_exact_roles_and_session_review(monkeypatch):
    import cr_web_actions as X,cr_google_chat as H
    monkeypatch.setattr(X.db,'kv_get',lambda *a:None)
    monkeypatch.setattr(X.db,'kv_set',lambda *a:None)
    monkeypatch.setattr(X.G,'encrypt',lambda uid,p:p)
    monkeypatch.setattr(H,'classify',lambda t:{'action':'draft','to':['sam@example.com'],'cc':[],'bcc':[],'subject':'Domain','body':'Please buy a .tech domain.'})
    seen=[];monkeypatch.setattr(X,'preview',lambda uid,h,b:seen.append((uid,h,b)) or {'review_id':'ticket'})
    assert X.compose_preview(10,'Bearer token',{'message':'Write a mail to sam@example.com asking for a domain'})=={'review_id':'ticket'}
    assert seen[0][1]=='Bearer token' and seen[0][2]['kind']=='email'

def test_send_decision_receipt_is_append_only_without_content(config):
    import hashlib
    data={'kind':'email','payload':{'id':'draft','hash':'short'},'text':'Private email body never audit'}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    row={'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest}
    seen=[]
    def q(sql,p=(),fetch='all'):
        seen.append((sql,p))
        if sql.startswith('UPDATE web_action_reviews') and "'claimed'" in sql:return row
    config.setattr(X.db,'q',q)
    def send(*a,**kw):
        assert kw['return_receipt'] is True
        assert any(sql.startswith('INSERT INTO web_send_decisions') and p[-2]=='accepted' for sql,p in seen)
        return {'text':'Sent','message_id':'gmail-real-id'}
    config.setattr(X.G,'send_draft',send)
    X.confirm(17,'Bearer '+'a'*43,{'review_id':'r','hash':digest,'decision':'confirm'})
    inserts=[p for sql,p in seen if sql.startswith('INSERT INTO web_send_decisions')]
    assert inserts==[('r',digest,17,A.digest('a'*43),'confirm','accepted',None),('r',digest,17,A.digest('a'*43),'confirm','sent','gmail-real-id')]
    assert all('Private email body' not in str(p) and 'a'*43 not in str(p) for p in inserts)
    assert not any(sql.startswith(('UPDATE web_send_decisions','DELETE FROM web_send_decisions')) for sql,p in seen)

def test_send_decision_audit_failure_blocks_send(config):
    import hashlib
    data={'kind':'email','payload':{'id':'draft','hash':'short'},'text':'Body'}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    row={'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest}
    def q(sql,p=(),fetch='all'):
        if sql.startswith('UPDATE web_action_reviews') and "'claimed'" in sql:return row
        if sql.startswith('INSERT INTO web_send_decisions'):raise RuntimeError('Audit unavailable')
    config.setattr(X.db,'q',q);config.setattr(X.G,'send_draft',lambda *a,**kw:pytest.fail('send blocked before audit'))
    with pytest.raises(RuntimeError):X.confirm(17,'Bearer '+'a'*43,{'review_id':'r','hash':digest,'decision':'confirm'})

def test_cancel_decision_retained_without_send(config):
    import hashlib
    data={'kind':'email','payload':{'id':'draft','hash':'short'},'text':'Body'}
    digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()
    row={'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest};seen=[]
    def q(sql,p=(),fetch='all'):
        if sql.startswith('UPDATE web_action_reviews') and "'claimed'" in sql:return row
        if sql.startswith('INSERT INTO web_send_decisions'):seen.append(p)
    config.setattr(X.db,'q',q);config.setattr(X.G,'cancel_draft',lambda *a,**kw:'Cancelled')
    config.setattr(X.G,'send_draft',lambda *a,**kw:pytest.fail('cancel cannot send'))
    X.confirm(17,'Bearer '+'a'*43,{'review_id':'r','hash':digest,'decision':'cancel'})
    assert [p[-2] for p in seen]==['accepted','cancelled']
