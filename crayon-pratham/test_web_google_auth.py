import json,time
from urllib.parse import parse_qs,urlsplit
import pytest,jwt
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric import rsa
import cr_web_google_auth as G
import cr_web_auth as A
@pytest.fixture
def env(monkeypatch):
    monkeypatch.setenv('GOOGLE_WEB_LOGIN_CLIENT_ID','client')
    monkeypatch.setenv('GOOGLE_WEB_LOGIN_CLIENT_SECRET','secret')
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setattr(G.db,'available',lambda:True)
    return monkeypatch

def test_start_login_only_scopes_pkce_cookie(env):
    data={'state':'a'*43,'verifier':'v'*48,'nonce':'nonce'};calls=[]
    env.setattr(G.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or {'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode()})
    url,cookie=G.start('a'*43);q=parse_qs(urlsplit(url).query)
    assert q['scope']==['openid email profile'] and 'access_type' not in q
    assert q['code_challenge']==[A.challenge(data['verifier'])]
    assert calls[0][1]==(A.digest(cookie),A.digest('a'*43))
    assert 'cookie_hash IS NULL' in calls[0][0]

def test_google_token_strict_subject_not_email(env):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    env.setattr(G.GoogleJWKClient,'get_signing_key_from_jwt',lambda self,t:type('K',(),{'key':key.public_key()})())
    base={'iss':'https://accounts.google.com','aud':'client','sub':'unique-sub','email':'test@example.com','email_verified':True,'nonce':'nonce','iat':int(time.time()),'exp':int(time.time())+60}
    assert G.validate(jwt.encode(base,key,algorithm='RS256'),'nonce')['subject']=='unique-sub'
    for change in [{'iss':'evil'},{'aud':'evil'},{'nonce':'evil'},{'iat':1},{'exp':1},{'email_verified':False},{'sub':17},{'azp':'evil'}]:
        with pytest.raises((A.AuthError,jwt.InvalidTokenError)):G.validate(jwt.encode({**base,**change},key,algorithm='RS256'),'nonce')
    with pytest.raises(jwt.InvalidAlgorithmError):G.validate(jwt.encode(base,'x'*32,algorithm='HS256'),'nonce')

def test_callback_replay_rejected_without_token_exchange(env):
    env.setattr(G.db,'q',lambda *a,**k:None)
    env.setattr(G.httpx,'post',lambda *a,**k:pytest.fail('must not exchange'))
    with pytest.raises(A.AuthError):G.callback('a'*43,'code','b'*43)

def test_begin_link_bound_to_authenticated_session(env):
    calls=[];env.setattr(G.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)))
    G.begin('a'*43,{'user_id':17,'name':'N'},'Bearer '+'t'*43)
    row=next(p for sql,p in calls if sql.startswith('INSERT'))
    data=json.loads(A._cipher().decrypt(row[2].encode()))
    assert data['uid']==17 and data['session']==A.digest('t'*43)
    assert 'email' not in data

def test_confirm_cannot_overwrite_identity(env):
    data={'subject':'s','uid':17,'email':'a@b.com','telegram_name':'N','google_name':'G'}
    digest=G.hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest();calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        return {'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest} if sql.startswith('DELETE FROM web_google_reviews') else None
    env.setattr(G.db,'q',q)
    import cr_accounts
    from test_accounts import Conn
    env.setattr(cr_accounts.db,'_conn',lambda:Conn())
    with pytest.raises(ValueError,match='already linked'):G.confirm({'user_id':17,'name':'N'},'Bearer '+'t'*43,{'review_id':'r'*43,'hash':digest,'decision':'confirm'})
    assert 'ON CONFLICT DO NOTHING' in calls[-1][0]
    assert calls[0][1][1:3]==(17,A.digest('t'*43))

def test_confirm_cancel_no_identity_write(env):
    calls=[]
    env.setattr(G.db,'q',lambda sql,p=(),fetch='all':calls.append(sql) or {'encrypted':'not needed'})
    r=G.confirm({'user_id':17},'Bearer '+'t'*43,{'review_id':'r'*43,'hash':'a'*64,'decision':'cancel'})
    assert 'Cancelled' in r['text'] and len(calls)==1

def test_new_google_uses_verified_subject_account_mapping(env):
    data={'state':'a'*43,'verifier':'v'*48,'nonce':'n'};calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        return {'challenge':'c'*43,'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode()} if sql.startswith('DELETE FROM web_google_states') else None
    env.setattr(G.db,'q',q)
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'id_token':'token'}
    env.setattr(G.httpx,'post',lambda *a,**k:Response())
    env.setattr(G,'validate',lambda *a:{'subject':'google-sub','email':'existing@example.com','google_name':'N'})
    import cr_accounts
    identities=[];env.setattr(cr_accounts,'google_account',lambda i:identities.append(i) or {'user_id':10**15,'name':'N'})
    G.callback('a'*43,'code','b'*43)
    assert identities[0]['subject']=='google-sub'
    assert calls[-1][1][2]==10**15

def test_link_callback_requires_original_active_session(env):
    data={'state':'a'*43,'verifier':'v'*48,'nonce':'n','uid':17,'name':'N','session':'sessionhash'}
    def q(sql,p=(),fetch='all'):
        return {'challenge':'c'*43,'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode()} if sql.startswith('DELETE FROM web_google_states') else None
    env.setattr(G.db,'q',q)
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'id_token':'token'}
    env.setattr(G.httpx,'post',lambda *a,**k:Response())
    env.setattr(G,'validate',lambda *a:{'subject':'google-sub','email':'a@example.com','google_name':'N'})
    with pytest.raises(A.AuthError,match='session ended'):G.callback('a'*43,'code','b'*43)


def test_google_callback_accepts_inert_provider_fields(env):
    import cr_web_http as H
    from test_web_app import Handler
    called=[];env.setattr(G,'callback',lambda *a:called.append(a))
    h=Handler('/web/google/auth/callback?code=code&state='+('a'*43)+'&scope=openid+email+profile&authuser=0&prompt=select_account',{'Cookie':'crayon_google_oidc='+('b'*43)})
    H.handle(h,'GET');assert h.code==200 and called==[('a'*43,'code','b'*43)]


def test_google_callback_ignores_unknown_inert_fields(env):
    import cr_web_http as H
    from test_web_app import Handler
    called=[];env.setattr(G,'callback',lambda *a:called.append(a))
    h=Handler('/web/google/auth/callback?code=code&state='+('a'*43)+'&iss=https%3A%2F%2Faccounts.google.com&new_metadata=value',{'Cookie':'crayon_google_oidc='+('b'*43)})
    H.handle(h,'GET');assert h.code==200 and len(called)==1

@pytest.mark.parametrize('query',['error=access_denied&state=a','code=a&state=a&state=b','code=a','state=a','code=a&state=a&state='])
def test_google_auth_failures_friendly_no_exchange(env,query):
    import cr_web_http as H
    from test_web_app import Handler
    env.setattr(G,'callback',lambda *a:pytest.fail('Must not exchange invalid callback'))
    h=Handler('/web/google/auth/callback?'+query);H.handle(h,'GET')
    assert h.code==401 and h.out['Content-Type']=='text/html'
    assert b'Return to Crayon' in h.wfile.getvalue() and b'crayon-login-error' in h.wfile.getvalue()


def test_auth_error_html_escapes_script_content(env):
    import cr_web_http as H
    from test_web_app import Handler
    h=Handler('/web/google/auth/callback')
    H.reply(h,401,{'error':'</script><img src=x onerror=alert(1)>'})
    assert b'</script><img' not in h.wfile.getvalue()
    assert b'&lt;/script&gt;' in h.wfile.getvalue()
