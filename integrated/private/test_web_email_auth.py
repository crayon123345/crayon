import time,datetime
import jwt,pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import hashes,serialization
from cryptography import x509
from cryptography.x509.oid import NameOID
import cr_web_email_auth as E
import cr_web_auth as A

def test_email_identity_requires_signed_verified_password_provider(monkeypatch):
    key=rsa.generate_private_key(public_exponent=65537,key_size=2048)
    name=x509.Name([x509.NameAttribute(NameOID.COMMON_NAME,'test')]);now=datetime.datetime.now(datetime.timezone.utc)
    cert=x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key()).serial_number(1).not_valid_before(now-datetime.timedelta(days=1)).not_valid_after(now+datetime.timedelta(days=1)).sign(key,hashes.SHA256()).public_bytes(serialization.Encoding.PEM).decode()
    monkeypatch.setenv('FIREBASE_EMAIL_PROJECT_ID',E.PROJECT)
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'test':cert}
    import httpx
    monkeypatch.setattr(httpx,'get',lambda *a,**k:Response())
    base={'iss':'https://securetoken.google.com/'+E.PROJECT,'aud':E.PROJECT,'sub':'firebase-uid','iat':int(time.time()),'exp':int(time.time())+60,'auth_time':int(time.time()),'email':'test@example.test','email_verified':True,'firebase':{'sign_in_provider':'password'}}
    token=lambda p:jwt.encode(p,key,algorithm='RS256',headers={'kid':'test'})
    assert E.validate(token(base))['subject']=='firebase-uid'
    for c in [{'email_verified':False},{'aud':'other'},{'iss':'https://evil.test'},{'auth_time':1},{'sub':''},{'firebase':{'sign_in_provider':'google.com'}}]:
        with pytest.raises(A.AuthError):E.validate(token({**base,**c}))
    with pytest.raises(A.AuthError):E.validate(jwt.encode(base,'x'*32,algorithm='HS256',headers={'kid':'test'}))

def test_email_disabled_gate_no_account_creation(monkeypatch):
    monkeypatch.delenv('CRAYON_EMAIL_AUTH_ENABLED',raising=False)
    monkeypatch.setattr(E,'validate',lambda *a:pytest.fail('disabled flow'))
    with pytest.raises(A.AuthError,match='being set up'):E.exchange({'id_token':'test'})


def test_email_token_backend_never_accepts_password(monkeypatch):
    with pytest.raises(ValueError):E.exchange({'email':'x','password':'secret'})


def test_verified_email_uses_provider_subject_not_address(monkeypatch):
    import cr_accounts
    called=[]
    monkeypatch.setattr(E,'config',lambda:{})
    monkeypatch.setattr(E,'admin_check',lambda t:{'auth_time':123})
    monkeypatch.setattr(E,'validate',lambda t:{'subject':'firebase-uid','email':'same@example.test','name':'N'})
    monkeypatch.setattr(cr_accounts,'identity_account',lambda p,i:called.append((p,i)) or {'user_id':10**15,'name':'N'})
    from test_accounts import Conn
    monkeypatch.setattr(cr_accounts.db,'_conn',lambda:Conn())
    monkeypatch.setattr(E,'init',lambda:None)
    monkeypatch.setattr(cr_accounts.db,'q',lambda *a:None)
    monkeypatch.setattr(A,'_new_session',lambda r:{**r,'token':'t'*43})
    assert E.exchange({'id_token':'test'})['user_id']==10**15
    assert called[0][0]=='firebase_email' and called[0][1]['subject']=='firebase-uid'

def test_revoked_email_session_deleted(monkeypatch):
    import cr_db as db
    from firebase_admin import auth
    calls=[]
    monkeypatch.setattr(E,'init',lambda:None)
    monkeypatch.setattr(E,'admin_app',lambda:None)
    monkeypatch.setattr(auth,'get_user',lambda *a,**k:type('User',(),{'disabled':False,'email_verified':True,'tokens_valid_after_timestamp':200000})())
    def q(sql,p=(),fetch='all'):
        calls.append(sql)
        if sql.startswith('SELECT'):return {'subject':'uid','auth_time':100}
    monkeypatch.setattr(db,'q',q)
    with pytest.raises(A.AuthError,match='revoked'):E.session_check('hash')
    assert sum(s.startswith('DELETE') for s in calls)==2


def test_email_session_source_failure_closed(monkeypatch):
    import cr_db as db
    from firebase_admin import auth
    monkeypatch.setattr(E,'init',lambda:None)
    monkeypatch.setattr(E,'admin_app',lambda:None)
    monkeypatch.setattr(db,'q',lambda *a:{'subject':'uid','auth_time':100})
    monkeypatch.setattr(auth,'get_user',lambda *a,**k:(_ for _ in ()).throw(RuntimeError()))
    with pytest.raises(A.AuthError,match='unavailable'):E.session_check('hash')

def test_email_link_review_bound_to_session_and_exact_text(monkeypatch):
    import cr_db as db
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setattr(E,'config',lambda:{})
    monkeypatch.setattr(E,'validate',lambda *a:{'subject':'uid','email':'review@example.test','name':''})
    monkeypatch.setattr(E,'admin_check',lambda *a:{'auth_time':int(time.time())})
    calls=[];monkeypatch.setattr(db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)))
    r=E.link_preview({'user_id':17,'name':'Legacy'},'Bearer '+'t'*43,{'id_token':'test'})
    insert=next(p for s,p in calls if s.startswith('INSERT'))
    assert insert[1:3]==(17,A.digest('t'*43))
    assert 'No existing account data will be merged' in r['text'] and 'review@example.test' in r['text']


def test_email_link_existing_identity_requires_merge_review(monkeypatch):
    import cr_db as db,json,hashlib
    from cryptography.fernet import Fernet
    from test_accounts import Conn
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    monkeypatch.setattr(db,'_conn',lambda:Conn())
    monkeypatch.setattr(E,'admin_app',lambda:None)
    from firebase_admin import auth
    monkeypatch.setattr(auth,'get_user',lambda *a,**k:type('User',(),{'disabled':False,'email_verified':True,'email':'x','tokens_valid_after_timestamp':0})())
    data={'identity':{'subject':'uid','email':'x'},'text':'Exact'};digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest();calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append(sql)
        if sql.startswith('DELETE FROM web_email_reviews'):return {'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'content_hash':digest}
        if sql.startswith('SELECT user_id'):return {'user_id':10**15}
    monkeypatch.setattr(db,'q',q)
    with pytest.raises(ValueError,match='reviewed merge'):E.link_confirm({'user_id':17},'Bearer '+'t'*43,{'review_id':'r'*43,'hash':digest,'decision':'confirm'})
    assert not any(s.startswith('INSERT INTO account_identities') for s in calls)
