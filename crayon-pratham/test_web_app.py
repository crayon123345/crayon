import io
import json
from urllib.parse import parse_qs, urlsplit
import pytest
from cryptography.fernet import Fernet
import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
import cr_web_auth as A
import cr_web_http as H
import cr_web_app as W
import cr_channel
import cr_tools as T

@pytest.fixture
def config(monkeypatch):
    monkeypatch.setenv('TELEGRAM_OIDC_CLIENT_ID','123456')
    monkeypatch.setenv('TELEGRAM_OIDC_CLIENT_SECRET','private-client-secret')
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setattr(A.db,'available',lambda:True)
    return monkeypatch

class Handler:
    def __init__(self,path,headers=None):self.path=path;self.headers=headers or {};self.wfile=io.BytesIO();self.out={};self.code=None
    def send_response(self,n):self.code=n
    def send_header(self,k,v):self.out[k]=v
    def end_headers(self):pass

def test_login_disabled_no_fake_auth(monkeypatch):
    monkeypatch.delenv('TELEGRAM_OIDC_CLIENT_ID',raising=False)
    with pytest.raises(A.AuthError):A.begin('a'*43)

def test_start_narrow_scopes_state_nonce_pkce(config):
    calls=[];config.setattr(A.db,'q',lambda *a,**kw:calls.append((a,kw)))
    url,cookie=A.begin('a'*43);q=parse_qs(urlsplit(url).query)
    assert q['scope']==['openid profile'] and q['code_challenge_method']==['S256']
    assert q['nonce'] and q['state'] and 'private-client-secret' not in url
    assert calls[-1][0][1][1]==A.digest(cookie)
    decoded=json.loads(A._cipher().decrypt(calls[-1][0][1][3].encode()))
    assert q['code_challenge']==[A.challenge(decoded['verifier'])]

@pytest.mark.parametrize('bad',['http://itsppm76.github.io','https://itsppm76.github.io/crayon','https://evil.test/path','https://u:p@evil.test','https://evil.test:444'])
def test_bad_origin_configuration(monkeypatch,bad):
    monkeypatch.setenv('CRAYON_WEB_ORIGIN',bad)
    with pytest.raises(A.AuthError):A.origin()

def test_bad_login_challenge(config):
    with pytest.raises(A.AuthError):A.begin('not a challenge')

def test_callback_browser_bound_atomic_consumption(config):
    captured=[]
    config.setattr(A.db,'q',lambda sql,p=(),fetch='all':captured.append((sql,p)) or None)
    with pytest.raises(A.AuthError):A.callback('a'*43,'code','b'*43)
    assert captured[0][1]==(A.digest('a'*43),A.digest('b'*43))
    assert 'DELETE' in captured[0][0] and 'expires_at>now()' in captured[0][0]

def test_exchange_requires_verifier_one_use(config):
    captured=[]
    config.setattr(A.db,'q',lambda sql,p=(),fetch='all':captured.append((sql,p)) or None)
    with pytest.raises(A.AuthError):A.exchange('a'*43,'b'*43)
    assert captured[0][1]==(A.digest('a'*43),A.challenge('b'*43))
    assert 'DELETE' in captured[0][0]

def test_exchange_stores_only_hashed_token(config):
    calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p));return {'user_id':17,'name':'Person'} if sql.startswith('DELETE FROM web_login_codes WHERE code_hash') else None
    config.setattr(A.db,'q',q);config.setattr(A.db,'audit',lambda *a:None)
    r=A.exchange('a'*43,'b'*43)
    stored=next(p for sql,p in calls if sql.startswith('INSERT INTO web_sessions'))
    assert stored[0]==A.digest(r['token']) and stored[0]!=r['token']
    assert r['user']['id']==17 and r['expires_in']==604800

def test_session_never_takes_client_uid(config):
    import cr_web_email_auth
    config.setattr(cr_web_email_auth,'session_check',lambda *a:None)
    calls=[];config.setattr(A.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or {'user_id':17,'name':'N'})
    assert A.session('Bearer '+'b'*43)['user_id']==17
    assert calls[0][1]==(A.digest('b'*43),)
    for h in ('','17','Bearer x'):
        with pytest.raises(A.AuthError):A.session(h)

def test_jwt_profile_id_not_sub(config):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    config.setattr(A.jwt.PyJWKClient,'get_signing_key_from_jwt',lambda self,t:type('K',(),{'key':key.public_key()})())
    import time
    claims={'iss':A.ISSUER,'aud':'123456','sub':'987987987','id':17,'iat':int(time.time()),'exp':int(time.time())+100,'nonce':'nonce','name':'N'}
    t=jwt.encode(claims,key,algorithm='RS256')
    assert A.validate_id_token(t,'nonce')==(17,'N')
    assert A.validate_id_token(jwt.encode({**claims,'id':'17'},key,algorithm='RS256'),'nonce')==(17,'N')
    with pytest.raises(A.AuthError):A.validate_id_token(t,'wrong')
    for changed in ({'aud':'wrong'},{'iss':'evil'},{'exp':1},{'iat':1},{'id':True},{'id':'17x'},{'id':17.0},{'id':'-17'},{'id':'017'}):
        with pytest.raises((A.AuthError,jwt.InvalidTokenError)):
            A.validate_id_token(jwt.encode({**claims,**changed},key,algorithm='RS256'),'nonce')
    with pytest.raises(jwt.InvalidAlgorithmError):
        A.validate_id_token(jwt.encode(claims,'x'*32,algorithm='HS256'),'nonce')

def test_cors_no_wildcard_or_credentials(config):
    h=Handler('/web/status',{'Origin':'https://evil.test'})
    H.handle(h,'GET');assert h.code==403 and 'Access-Control-Allow-Origin' not in h.out
    h=Handler('/web/status',{'Origin':A.origin()})
    H.handle(h,'GET');assert h.code==200 and h.out['Access-Control-Allow-Origin']==A.origin()
    assert 'Access-Control-Allow-Credentials' not in h.out

def test_preflight(config):
    h=Handler('/web/chat',{'Origin':A.origin()});H.handle(h,'OPTIONS')
    assert h.code==204 and h.out['Access-Control-Allow-Headers']=='Authorization, Content-Type'

def test_private_routes_require_session(config):
    config.setattr(A.db,'q',lambda *a,**kw:None)
    for path in ('/web/history','/web/activity','/web/me','/web/result?id='+'a'*43):
        h=Handler(path,{'Origin':A.origin()});H.handle(h,'GET');assert h.code==401

def test_history_scope_from_session(config):
    calls=[];config.setattr(A,'session',lambda h:{'user_id':17,'name':'N'})
    config.setattr(W,'history',lambda uid,before=None:calls.append((uid,before)) or {'messages':[]})
    h=Handler('/web/history?uid=99',{'Origin':A.origin(),'Authorization':'Bearer x'})
    H.handle(h,'GET');assert calls==[] and h.code==400
    h=Handler('/web/history?before=90',{'Origin':A.origin(),'Authorization':'Bearer x'})
    H.handle(h,'GET');assert calls==[(17,'90')] and h.code==200

def test_chat_cannot_supply_uid(config):
    config.setattr(A,'session',lambda h:{'user_id':17,'name':'N'})
    h=Handler('/web/chat',{'Origin':A.origin(),'Content-Type':'application/json'})
    H.handle(h,'POST',json.dumps({'message':'Hi','request_id':'a'*43,'uid':99}).encode());assert h.code==400

def test_web_tools_enforced_even_if_model_requests(config):
    mark=cr_channel.channel.set('web')
    try:
        names=[x['name'] for x in T.declarations()[0]['functionDeclarations']]
        assert 'remember' in names and 'create_task' in names
        for name in ('forget',):
            assert name not in names and not T.run(name,{}, {'uid':17,'chat_id':17})['ok']
    finally:cr_channel.channel.reset(mark)
    assert 'computer_browse' in [x['name'] for x in T.declarations()[0]['functionDeclarations']]

def test_secret_rejected_before_storage(config):
    config.setattr(W.db,'q',lambda *a,**k:pytest.fail('secret must not be stored'))
    with pytest.raises(ValueError):W.submit({'user_id':17,'name':'N'},{'message':'password: veryprivate123','request_id':'a'*43})

def test_duplicate_not_rerun(config):
    config.setattr(W.db,'q',lambda *a,**k:{'state':'done'})
    config.setattr(W.POOL,'submit',lambda *a:pytest.fail('no rerun'))
    assert W.submit({'user_id':17,'name':'N'},{'message':'hello','request_id':'a'*43})['state']=='done'

def test_result_cross_user_fail_closed(config):
    calls=[];config.setattr(W.db,'q',lambda sql,p=(),fetch='all':calls.append(p) or None)
    with pytest.raises(ValueError):W.result(17,'a'*43)
    assert calls==[(17,'a'*43)]

def test_artifacts_bounded_plain_output():
    o=W.WebOut();o.send(17,'<script>alert(1)</script>')
    assert o.items[0]['text'].startswith('<script>') # frontend renders with textContent, not HTML
    o.artifact(17,{'filename':'../../a.csv','mime':'text/csv','data':b'x,y'})
    assert o.items[1]['name']=='a.csv'
    with pytest.raises(ValueError):o.artifact(17,{'filename':'big','mime':'text/plain','data':b'x'*2000001})

def test_dispatch_does_not_use_telegram_confirmation(config):
    import cr_memory as M,cr_agent as Brain
    config.setattr(W.db,'q',lambda *a,**k:{'user_id':17})
    config.setattr(M,'touch_user',lambda *a:None)
    config.setattr(Brain,'respond',lambda *a,**k:pytest.fail('must not run model/confirmation'))
    for s in ('yes','send it','connect Google','read my inbox','forget name'):
        assert W.dispatch(17,'N',s)[0]['kind']=='text'

def test_frontend_session_browser_storage_and_revocation():
    from pathlib import Path
    s=Path('docs/app.js').read_text()
    assert 'localStorage' in s and 'sessionStorage' not in s
    assert "await call('me')" in s and 'saved.expires<=Date.now()' in s
    assert 'localStorage.removeItem' in s
    assert "addEventListener('storage'" in s
    assert "e.origin!==API||e.source!==popup" in s
    assert '.innerHTML' not in s
    assert 'session_id' not in s


def test_jwks_gzip_decoded_before_json(config):
    import gzip, httpx
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    payload = {'keys': [json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(key.public_key()))]}
    client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(
        200, headers={'Content-Encoding':'gzip','Content-Type':'application/json'},
        content=gzip.compress(json.dumps(payload).encode()))))
    config.setattr(A.httpx,'get',lambda *args,**kwargs:client.get('https://oauth.telegram.org/.well-known/jwks.json'))
    assert A.TelegramJWKClient(A.ISSUER+'/.well-known/jwks.json').fetch_data()==payload


def test_jwks_decode_errors_not_exposed(config):
    class Bad:
        def raise_for_status(self): pass
        def json(self): raise UnicodeDecodeError('utf-8',b'\x1f\x8b',1,2,'invalid start byte')
    config.setattr(A.httpx,'get',lambda *a,**k:Bad())
    with pytest.raises(A.AuthError,match='signing keys could not be checked'):
        A.TelegramJWKClient(A.ISSUER+'/.well-known/jwks.json').fetch_data()


def test_provider_decode_error_safe_http(config):
    def fail(*a): raise UnicodeDecodeError('utf-8',b'\x1f\x8b',1,2,'invalid start byte')
    config.setattr(A,'callback',fail)
    h=Handler('/web/auth/callback?code=x&state=y')
    H.handle(h,'GET')
    assert h.code==503
    assert b'invalid start byte' not in h.wfile.getvalue() and b'Start login again' in h.wfile.getvalue()
    assert h.out['Content-Type']=='text/html'


def test_poll_login_bound_to_verifier(config):
    calls=[]
    config.setattr(A.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or None)
    assert A.poll_login('a'*43)=={'pending':True}
    assert calls[0][1]==(A.challenge('a'*43),)
    assert 'DELETE' in calls[0][0] and 'expires_at>now()' in calls[0][0]
    with pytest.raises(A.AuthError):A.poll_login('guess')


def test_poll_login_one_use_session(config):
    calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        return {'user_id':17,'name':'N'} if sql.startswith('DELETE FROM web_login_codes WHERE challenge=') else None
    config.setattr(A.db,'q',q);config.setattr(A.db,'audit',lambda *a:None)
    r=A.poll_login('a'*43)
    assert r['user']['id']==17
    assert next(p for sql,p in calls if sql.startswith('INSERT INTO web_sessions'))[0]==A.digest(r['token'])


def test_poll_exact_origin_and_shape(config):
    config.setattr(A,'poll_login',lambda v:{'pending':True})
    h=Handler('/web/login-poll',{'Origin':A.origin(),'Content-Type':'application/json'})
    H.handle(h,'POST',b'{"verifier":"a"}');assert h.code==200
    h=Handler('/web/login-poll',{'Origin':'https://evil.test','Content-Type':'application/json'})
    H.handle(h,'POST',b'{"verifier":"a"}');assert h.code==403
    h=Handler('/web/login-poll',{'Origin':A.origin(),'Content-Type':'application/json'})
    H.handle(h,'POST',b'{"verifier":"a","uid":17}');assert h.code==400


def test_history_pagination_bound_and_media_placeholder(config):
    calls=[]
    from datetime import datetime,timezone,timedelta
    rows=[{'kind':'message','id':str(i),'role':'user','content':'[User sent media: image/png]','ts':datetime(2026,10,9,tzinfo=timezone.utc)+timedelta(seconds=i)} for i in range(100,49,-1)]
    config.setattr(W.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or rows)
    r=W.history(17,'2026-10-10T00:00:00+00:00')
    assert len(r['messages'])==50 and r['has_more'] and r['before']==rows[49]['ts'].isoformat()
    assert calls[0][1][0]==17 and calls[0][1][2]==17 and all(m['media_missing'] for m in r['messages'])
    with pytest.raises(ValueError):W.history(17,'1 OR 1=1')


def test_upload_validation_and_replay(config):
    import base64
    body={'data':base64.b64encode(b'hello').decode(),'mime':'text/plain','name':'a.txt','caption':'read it','request_id':'a'*43}
    config.setattr(W.db,'q',lambda *a,**k:{'state':'done'})
    config.setattr(W.POOL,'submit',lambda *a:pytest.fail('duplicate must not process'))
    assert W.upload({'user_id':17,'name':'N'},body)['state']=='done'
    for change in ({'data':'invalid$$'},{'caption':'password: veryprivate123'},{'data':'a'*26666673},{'uid':99}):
        with pytest.raises(ValueError):W.upload({'user_id':17,'name':'N'},{**body,**change})


def test_upload_raw_bytes_not_stored(config):
    import base64
    calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        if sql.startswith('SELECT state'):return None
        if 'AS n' in sql:return {'n':0}
        if sql.startswith('INSERT'):return {'id':'a'*43}
    config.setattr(W.db,'q',q);config.setattr(W.POOL,'submit',lambda *a:None)
    config.setattr(W,'encode',lambda v:json.dumps(v))
    body={'data':base64.b64encode(b'RAW_UPLOAD_BYTES').decode(),'mime':'text/plain','name':'a.txt','caption':'read','request_id':'a'*43}
    try:
        W.upload({'user_id':17,'name':'N'},body)
        stored=next(p for sql,p in calls if sql.startswith('INSERT'))
        assert 'RAW_UPLOAD_BYTES' not in str(stored) and body['data'] not in str(stored)
        assert 'raw_retained' in str(stored)
    finally:W.SLOTS.release()


def test_sync_and_wait_ui():
    from pathlib import Path
    s=Path('docs/app.js').read_text()
    assert 'typing-bubble' in s and 'Still working. Your request is not being repeated.' in s
    assert '30000' in s and 'historyPaged' in s and 'passive&&key===lastHistoryKey' in s


def test_web_reply_mirrors_only_authenticated_uid(config):
    import cr_telegram
    sent=[]
    class Out:
        def send(self,uid,text):sent.append((uid,text))
    config.setattr(cr_telegram,'Out',Out)
    config.setattr(W,'decode',lambda v:{'input':'hello','name':'N'})
    config.setattr(W,'encode',lambda v:json.dumps(v))
    config.setattr(W,'dispatch',lambda *a:[{'kind':'text','text':'reply'}])
    config.setattr(W.db,'q',lambda sql,p=(),fetch='all':{'encrypted':'data'} if sql.startswith("UPDATE web_requests SET state='running'") else None)
    W.SLOTS.acquire()
    W._run(17,'a'*43)
    assert sent==[(17,'[From web] hello'),(17,'reply')]


def test_web_private_command_not_mirrored(config):
    import cr_telegram
    class Out:
        def __init__(self):pytest.fail('private command must not mirror')
    config.setattr(cr_telegram,'Out',Out)
    config.setattr(W,'decode',lambda v:{'input':'read my inbox','name':'N'})
    config.setattr(W,'encode',lambda v:json.dumps(v))
    config.setattr(W,'dispatch',lambda *a:[{'kind':'text','text':'private'}])
    config.setattr(W.db,'q',lambda sql,p=(),fetch='all':{'encrypted':'data'} if sql.startswith("UPDATE web_requests SET state='running'") else None)
    W.SLOTS.acquire();W._run(17,'a'*43)


def test_mirror_failure_not_retried(config):
    import cr_telegram
    sent=[];writes=[]
    class Out:
        def send(self,*a):sent.append(a);raise RuntimeError('transport')
    config.setattr(cr_telegram,'Out',Out)
    config.setattr(W,'decode',lambda v:{'input':'hello','name':'N'})
    config.setattr(W,'encode',lambda v:json.dumps(v))
    config.setattr(W,'dispatch',lambda *a:[{'kind':'text','text':'reply'}])
    def q(sql,p=(),fetch='all'):
        writes.append(p)
        return {'encrypted':'data'} if sql.startswith("UPDATE web_requests SET state='running'") else None
    config.setattr(W.db,'q',q)
    W.SLOTS.acquire();W._run(17,'a'*43)
    assert len(sent)==1 and 'No automatic resend' in str(writes)

def test_browser_only_account_never_mirrors_to_telegram(config):
    import cr_telegram
    config.setattr(cr_telegram,'Out',lambda:pytest.fail('No Telegram route'))
    config.setattr(W,'decode',lambda v:{'input':'hello','name':'N'})
    config.setattr(W,'encode',lambda v:json.dumps(v))
    config.setattr(W,'dispatch',lambda *a:[{'kind':'text','text':'reply'}])
    config.setattr(W.db,'q',lambda sql,p=(),fetch='all':{'encrypted':'data'} if sql.startswith("UPDATE web_requests SET state='running'") else None)
    W.SLOTS.acquire();W._run(10**15,'a'*43)


def test_browser_only_reminder_creates_inapp_delivery(config):
    stored={}
    def q(sql,p=(),*args,**kwargs):
        if sql.startswith('INSERT INTO reminders'):
            stored['due']=p[3];return {'id':1}
        if sql.startswith('SELECT due_at'):return {'due_at':stored['due'],'status':'pending'}
    config.setattr(T.db,'q',q)
    r=T.set_reminder({'uid':10**15,'chat_id':10**15},'Test',in_minutes=5)
    assert r['ok']

def test_content_title_is_bounded_and_no_secrets():
    import cr_conversations as C
    assert C.title_from_text('Can you help me plan a study session?')=='Plan a study session'
    assert len(C.title_from_text('word '*100))<=70
    assert C.title_from_text('') is None
