"""Google login with provider-independent accounts and reviewed legacy links. No email matching or data scopes."""
import hashlib,hmac,json,re,secrets,time
from urllib.parse import urlencode
import httpx,jwt
import cr_db as db
import cr_config as C
import cr_web_auth as A

SCHEMA='''CREATE TABLE IF NOT EXISTS web_google_identities(
 subject TEXT PRIMARY KEY,user_id BIGINT NOT NULL UNIQUE,email TEXT NOT NULL,name TEXT NOT NULL,linked_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS web_google_states(
 state_hash TEXT PRIMARY KEY,cookie_hash TEXT,challenge TEXT NOT NULL,encrypted TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS web_google_reviews(
 id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,session_hash TEXT NOT NULL,challenge TEXT NOT NULL,
 encrypted TEXT NOT NULL,content_hash TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL);'''
def init():
    for stmt in SCHEMA.split(';'):
        if stmt.strip():db.q(stmt,fetch='none')
def configured():return bool(C.env('GOOGLE_WEB_LOGIN_CLIENT_ID') and C.env('GOOGLE_WEB_LOGIN_CLIENT_SECRET') and C.env('CRAYON_WEB_ENCRYPTION_KEY') and db.available())
def redirect():return C.PUBLIC_URL.rstrip('/')+'/web/google/auth/callback'
def begin(challenge,user=None,header=None):
    if not configured():raise A.AuthError('Google login setup is not active yet.')
    if not isinstance(challenge,str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}',challenge):raise A.AuthError('Invalid login request.')
    init();A.origin();db.q('DELETE FROM web_google_reviews WHERE expires_at<now()',fetch='none');state=secrets.token_urlsafe(32)
    data={'state':state,'verifier':secrets.token_urlsafe(48),'nonce':secrets.token_urlsafe(32)}
    if user:data.update(uid=user['user_id'],name=user['name'],session=A.digest(header[7:]))
    db.q("DELETE FROM web_google_states WHERE expires_at<now()",fetch='none')
    db.q("INSERT INTO web_google_states(state_hash,challenge,encrypted,expires_at) VALUES(%s,%s,%s,now()+interval '5 minutes')",(A.digest(state),challenge,A._cipher().encrypt(json.dumps(data).encode()).decode()),'none')
    return C.PUBLIC_URL.rstrip('/')+'/web/google/auth/start?state='+state

def start(state):
    if not isinstance(state,str) or not A.PATTERN.fullmatch(state):raise A.AuthError('Invalid login start.')
    cookie=secrets.token_urlsafe(32)
    row=db.q('UPDATE web_google_states SET cookie_hash=%s WHERE state_hash=%s AND cookie_hash IS NULL AND expires_at>now() RETURNING encrypted',(A.digest(cookie),A.digest(state)),'one')
    if not row:raise A.AuthError('Login expired or already started.')
    data=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({'client_id':C.env('GOOGLE_WEB_LOGIN_CLIENT_ID'),'redirect_uri':redirect(),'response_type':'code','scope':'openid email profile','prompt':'select_account','state':state,'nonce':data['nonce'],'code_challenge':A.challenge(data['verifier']),'code_challenge_method':'S256'}),cookie

class GoogleJWKClient(A.TelegramJWKClient):
    def fetch_data(self):
        try:
            r=httpx.get('https://www.googleapis.com/oauth2/v3/certs',timeout=10,follow_redirects=False);r.raise_for_status();payload=r.json()
            if not isinstance(payload,dict) or not isinstance(payload.get('keys'),list):raise ValueError()
            if self.jwk_set_cache is not None:self.jwk_set_cache.put(payload)
            return payload
        except Exception:raise A.AuthError('Google signing keys could not be checked.') from None

def validate(token,nonce):
    key=GoogleJWKClient('https://www.googleapis.com/oauth2/v3/certs').get_signing_key_from_jwt(token).key
    claims=jwt.decode(token,key,algorithms=['RS256'],audience=C.env('GOOGLE_WEB_LOGIN_CLIENT_ID'),options={'require':['exp','iat','iss','aud','sub','nonce','email','email_verified']},leeway=5)
    if claims['iss'] not in ('https://accounts.google.com','accounts.google.com') or claims.get('azp',C.env('GOOGLE_WEB_LOGIN_CLIENT_ID'))!=C.env('GOOGLE_WEB_LOGIN_CLIENT_ID'):raise A.AuthError('Wrong Google issuer or client.')
    if not isinstance(claims['nonce'],str) or not hmac.compare_digest(claims['nonce'],nonce) or not time.time()-300<=claims['iat']<=time.time()+5:raise A.AuthError('Google login binding expired or changed.')
    if claims['email_verified'] is not True or not isinstance(claims['sub'],str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,255}',claims['sub']) or not isinstance(claims['email'],str) or len(claims['email'])>320:raise A.AuthError('Google identity could not be verified.')
    return {'subject':claims['sub'],'email':claims['email'],'google_name':str(claims.get('name') or '')[:100]}

def callback(state,code,cookie):
    if not all(isinstance(x,str) and A.PATTERN.fullmatch(x) for x in (state,cookie)) or not isinstance(code,str) or not 1<=len(code)<=2048:raise A.AuthError('Invalid Google callback.')
    row=db.q('DELETE FROM web_google_states WHERE state_hash=%s AND cookie_hash=%s AND expires_at>now() RETURNING challenge,encrypted',(A.digest(state),A.digest(cookie)),'one')
    if not row:raise A.AuthError('Google login expired or already used.')
    data=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    if not hmac.compare_digest(data['state'],state):raise A.AuthError('State changed.')
    r=httpx.post('https://oauth2.googleapis.com/token',data={'client_id':C.env('GOOGLE_WEB_LOGIN_CLIENT_ID'),'client_secret':C.env('GOOGLE_WEB_LOGIN_CLIENT_SECRET'),'grant_type':'authorization_code','code':code,'redirect_uri':redirect(),'code_verifier':data['verifier']},timeout=20,follow_redirects=False)
    r.raise_for_status();identity=validate(r.json()['id_token'],data['nonce'])
    # Access/refresh tokens are not retained. Login grants no data integration permissions.
    if 'uid' in data:
        active=db.q('SELECT user_id FROM web_sessions WHERE token_hash=%s AND user_id=%s AND expires_at>now()',(data['session'],data['uid']),'one')
        if not active:raise A.AuthError('Original Crayon session ended. Link again while signed in.')
        payload={**identity,'uid':data['uid'],'telegram_name':data['name']};digest=hashlib.sha256(json.dumps(payload,sort_keys=True).encode()).hexdigest();ident=secrets.token_urlsafe(32)
        db.q("INSERT INTO web_google_reviews(id,user_id,session_hash,challenge,encrypted,content_hash,expires_at) VALUES(%s,%s,%s,%s,%s,%s,now()+interval '5 minutes')",(ident,data['uid'],data['session'],row['challenge'],A._cipher().encrypt(json.dumps(payload).encode()).decode(),digest),'none')
        return 'review'
    import cr_accounts
    linked=cr_accounts.google_account(identity)
    handoff=secrets.token_urlsafe(32)
    db.q("INSERT INTO web_login_codes(code_hash,challenge,user_id,name,expires_at) VALUES(%s,%s,%s,%s,now()+interval '5 minutes')",(A.digest(handoff),row['challenge'],linked['user_id'],linked['name']),'none')
    return handoff

def review(user,header,verifier):
    if not isinstance(verifier,str) or not A.PATTERN.fullmatch(verifier):raise ValueError('Invalid link verifier.')
    row=db.q('SELECT id,encrypted,content_hash FROM web_google_reviews WHERE user_id=%s AND session_hash=%s AND challenge=%s AND expires_at>now()',(user['user_id'],A.digest(header[7:]),A.challenge(verifier)),'one')
    if not row:return {'pending':True}
    data=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    return {'review_id':row['id'],'hash':row['content_hash'],'text':'Link Google login to this existing Crayon account?\nCrayon: '+str(user['name'])+' (ID '+str(user['user_id'])+')\nGoogle: '+data['email']+'\nThis will let that Google account open the same private memory and history. No Gmail, Calendar or Workspace access is granted.','google_email':data['email']}

def confirm(user,header,body):
    if set(body)!={'review_id','hash','decision'} or body['decision'] not in ('confirm','cancel') or not isinstance(body['review_id'],str) or not A.PATTERN.fullmatch(body['review_id']) or not isinstance(body['hash'],str) or not re.fullmatch(r'[a-f0-9]{64}',body['hash']):raise ValueError('Invalid linking decision.')
    row=db.q('DELETE FROM web_google_reviews WHERE id=%s AND user_id=%s AND session_hash=%s AND content_hash=%s AND expires_at>now() RETURNING encrypted,content_hash',(body['review_id'],user['user_id'],A.digest(header[7:]),body['hash']),'one')
    if not row:raise ValueError('Link review expired, changed, wrong session or already used.')
    if body['decision']=='cancel':return {'text':'Cancelled. Google login was not linked.'}
    data=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    if hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()!=row['content_hash'] or data['uid']!=user['user_id']:raise ValueError('Link review changed.')
    import cr_accounts
    cr_accounts.link_legacy_google(data,user['user_id'],user['name'])
    db.audit(user['user_id'],'web_google_link')
    return {'text':'Google login linked. Both sign-in methods now open this same Crayon memory and history.'}
