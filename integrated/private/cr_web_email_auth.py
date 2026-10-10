"""Verified Firebase email/password identities. No password touches this backend.
Enabled only with isolated project configuration and revocation checks.
"""
import time,jwt
import cr_web_auth as A
import cr_config as C

PROJECT='crayon-email-auth'
CERTS='https://www.googleapis.com/robot/v1/metadata/x509/securetoken@system.gserviceaccount.com'

def validate(token):
    if not isinstance(token,str) or not 1<len(token)<10000:raise A.AuthError('Invalid email sign-in token.')
    project=C.env('FIREBASE_EMAIL_PROJECT_ID')
    if project!=PROJECT:raise A.AuthError('Email sign-in is not active yet.')
    # x509 endpoint requires certificate-to-public-key conversion; verify all claims
    # only after checking the signature against Google's current published certificate.
    import httpx
    from cryptography import x509
    try:
        header=jwt.get_unverified_header(token)
        if header.get('alg')!='RS256' or not isinstance(header.get('kid'),str):raise ValueError()
        r=httpx.get(CERTS,timeout=10,follow_redirects=False);r.raise_for_status();certs=r.json()
        cert=certs.get(header['kid'])
        if not isinstance(cert,str):raise ValueError()
        key=x509.load_pem_x509_certificate(cert.encode()).public_key()
        claims=jwt.decode(token,key,algorithms=['RS256'],audience=project,issuer='https://securetoken.google.com/'+project,
            options={'require':['exp','iat','sub','auth_time','email','email_verified','firebase']},leeway=5)
        if not isinstance(claims['sub'],str) or not __import__('re').fullmatch(r'[A-Za-z0-9_-]{1,128}',claims['sub']):raise ValueError()
        if claims['email_verified'] is not True or claims['firebase'].get('sign_in_provider')!='password':raise ValueError()
        if not isinstance(claims['email'],str) or len(claims['email'])>320:raise ValueError()
        if not time.time()-300<=claims['auth_time']<=time.time()+5 or claims['iat']>time.time()+5:raise ValueError()
        return {'subject':claims['sub'],'email':claims['email'],'name':str(claims.get('name') or '')[:100]}
    except Exception:raise A.AuthError('Email sign-in could not be verified. Verify your email, then sign in again.') from None


def exchange(body):
    if set(body)!={'id_token'}:raise ValueError('Invalid email sign-in request.')
    config()
    identity=validate(body['id_token'])
    claims=admin_check(body['id_token'])
    import cr_accounts
    init()
    import cr_db as db
    with db._conn().transaction():
        row=cr_accounts.identity_account('firebase_email',identity)
        result=A._new_session(row)
        db.q('INSERT INTO web_email_sessions(token_hash,subject,auth_time,user_id) VALUES(%s,%s,%s,%s)',(A.digest(result['token']),identity['subject'],int(claims['auth_time']),row['user_id']),'none')
        return result


def config():
    if C.env('CRAYON_EMAIL_AUTH_ENABLED')!='on' or C.env('FIREBASE_EMAIL_PROJECT_ID')!=PROJECT or not C.env('FIREBASE_EMAIL_BROWSER_KEY'):
        raise A.AuthError('Email sign-in is being set up. Use Google or Telegram for now.')
    if not C.env('FIREBASE_EMAIL_SERVICE_ACCOUNT'):raise A.AuthError('Email sign-in security setup is not active yet.')
    return {'apiKey':C.env('FIREBASE_EMAIL_BROWSER_KEY'),'authDomain':PROJECT+'.firebaseapp.com','projectId':PROJECT}


def admin_app():
    import firebase_admin
    from firebase_admin import credentials
    import json
    try:return firebase_admin.get_app('crayon-email')
    except ValueError:
        data=json.loads(C.env('FIREBASE_EMAIL_SERVICE_ACCOUNT'))
        if data.get('project_id')!=PROJECT:raise A.AuthError('Wrong email-auth project.')
        return firebase_admin.initialize_app(credentials.Certificate(data),{'projectId':PROJECT},name='crayon-email')

def admin_check(token):
    try:
        from firebase_admin import auth
        return auth.verify_id_token(token,app=admin_app(),check_revoked=True,clock_skew_seconds=5)
    except Exception:raise A.AuthError('Email sign-in is revoked, disabled or could not be checked. Sign in again.') from None


def init():
    A.init()
    import cr_db as db
    db.q('CREATE TABLE IF NOT EXISTS web_email_reviews(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,session_hash TEXT NOT NULL,encrypted TEXT NOT NULL,content_hash TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL)',fetch='none')
    db.q('CREATE TABLE IF NOT EXISTS web_email_sessions(token_hash TEXT PRIMARY KEY,subject TEXT NOT NULL,auth_time BIGINT NOT NULL,user_id BIGINT NOT NULL)',fetch='none')
    db.q('DELETE FROM web_email_reviews WHERE expires_at<now()',fetch='none')
    db.q('DELETE FROM web_email_sessions WHERE token_hash NOT IN (SELECT token_hash FROM web_sessions)',fetch='none')

def session_check(token_hash):
    import cr_db as db
    init()
    row=db.q('SELECT subject,auth_time FROM web_email_sessions WHERE token_hash=%s',(token_hash,),'one')
    if not row:return
    try:
        from firebase_admin import auth
        user=auth.get_user(row['subject'],app=admin_app())
        if user.disabled or not user.email_verified or row['auth_time']*1000<user.tokens_valid_after_timestamp:
            db.q('DELETE FROM web_sessions WHERE token_hash=%s',(token_hash,),'none')
            db.q('DELETE FROM web_email_sessions WHERE token_hash=%s',(token_hash,),'none')
            raise A.AuthError('Email sign-in was revoked. Sign in again.')
    except A.AuthError:raise
    except Exception:raise A.AuthError('Email session security check unavailable. Try again later.') from None


def link_preview(user,header,body):
    if set(body)!={'id_token'}:raise ValueError('Invalid email link request.')
    config();identity=validate(body['id_token']);claims=admin_check(body['id_token']);identity['auth_time']=int(claims['auth_time'])
    import secrets,json,hashlib,cr_db as db
    init()
    text='Link this verified email login to your current Crayon account?\nCrayon: '+str(user['name'])+' (ID '+str(user['user_id'])+')\nEmail: '+identity['email']+'\nBoth logins will open this same memory and history. No existing account data will be merged or moved. Passwords stay with Firebase.'
    data={'identity':identity,'text':text};digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest();ident=secrets.token_urlsafe(32)
    db.q("INSERT INTO web_email_reviews(id,user_id,session_hash,encrypted,content_hash,expires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '5 minutes')",(ident,user['user_id'],A.digest(header[7:]),A._cipher().encrypt(json.dumps(data).encode()).decode(),digest),'none')
    return {'review_id':ident,'hash':digest,'text':text}

def link_confirm(user,header,body):
    if set(body)!={'review_id','hash','decision'} or not isinstance(body['review_id'],str) or not A.PATTERN.fullmatch(body['review_id']) or not isinstance(body['hash'],str) or not __import__('re').fullmatch(r'[a-f0-9]{64}',body['hash']) or body['decision'] not in ('confirm','cancel'):raise ValueError('Invalid link decision.')
    import cr_db as db,json,hashlib,cr_accounts
    init()
    with db._conn().transaction():
        row=db.q('DELETE FROM web_email_reviews WHERE id=%s AND user_id=%s AND session_hash=%s AND content_hash=%s AND expires_at>now() RETURNING encrypted,content_hash',(body['review_id'],user['user_id'],A.digest(header[7:]),body['hash']),'one')
        if not row:raise ValueError('Review expired, changed or already used.')
        if body['decision']=='cancel':return {'text':'Email linking cancelled. No account was changed.'}
        data=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
        if hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()!=row['content_hash']:raise ValueError('Review changed.')
        identity=data['identity'];subject=identity['subject']
        from firebase_admin import auth
        provider_user=auth.get_user(subject,app=admin_app())
        if provider_user.disabled or not provider_user.email_verified or provider_user.email!=identity['email'] or identity.get('auth_time',0)*1000<provider_user.tokens_valid_after_timestamp:raise ValueError('Email identity changed. Start the link review again.')
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',('crayon-firebase_email:'+subject,),'none')
        cr_accounts.init()
        existing=db.q("SELECT user_id FROM account_identities WHERE provider='firebase_email' AND subject=%s",(subject,),'one')
        if existing:raise ValueError('That email login already has a Crayon account. A separate reviewed merge is required; nothing moved.')
        linked=db.q("INSERT INTO account_identities(provider,subject,user_id,display_email) VALUES('firebase_email',%s,%s,%s) ON CONFLICT DO NOTHING RETURNING user_id",(subject,user['user_id'],identity['email']),'one')
        if not linked:raise ValueError('An email login is already linked to this account. Nothing was changed.')
        return {'text':'Email login linked to this same Crayon memory and history.'}
