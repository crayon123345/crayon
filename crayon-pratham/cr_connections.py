"""Separate per-user Workspace/GitHub OAuth. Never overwrites Gmail/calendar."""
import base64
import hashlib
import re
import secrets
from urllib.parse import urlencode
import httpx
import cr_config as C
import cr_db as db
import cr_google as G

GOOGLE_SCOPES=['openid','email','https://www.googleapis.com/auth/documents','https://www.googleapis.com/auth/spreadsheets','https://www.googleapis.com/auth/presentations']
PROVIDERS={'workspace','github'}


def init():
    db.q('''CREATE TABLE IF NOT EXISTS service_connections(user_id BIGINT,provider TEXT,
       identity TEXT NOT NULL,encrypted_tokens TEXT NOT NULL,connected_at TIMESTAMPTZ DEFAULT now(),
       PRIMARY KEY(user_id,provider))''',fetch='none')
    db.q('''CREATE TABLE IF NOT EXISTS service_oauth_states(state_hash TEXT PRIMARY KEY,user_id BIGINT,
       provider TEXT NOT NULL,expires_at TIMESTAMPTZ NOT NULL,used BOOLEAN DEFAULT false,encrypted_verifier TEXT)''',fetch='none')
    db.q('ALTER TABLE service_oauth_states ADD COLUMN IF NOT EXISTS encrypted_verifier TEXT',fetch='none')


def configured(provider):
    keys=('GOOGLE_WORKSPACE_CLIENT_ID','GOOGLE_WORKSPACE_CLIENT_SECRET') if provider=='workspace' else ('GITHUB_OAUTH_CLIENT_ID','GITHUB_OAUTH_CLIENT_SECRET')
    return provider in PROVIDERS and all(C.env(k) for k in (*keys,'GOOGLE_TOKEN_ENCRYPTION_KEY'))


def callback(provider):return C.PUBLIC_URL.rstrip('/')+'/connections/'+provider+'/callback'


def begin(uid,provider):
    if int(uid)<=0 or not configured(provider):raise G.GoogleError('This connection is not configured yet.')
    init();state=secrets.token_urlsafe(32);digest=hashlib.sha256(state.encode()).hexdigest()
    db.q("DELETE FROM service_oauth_states WHERE expires_at<now()",fetch='none')
    verifier=secrets.token_urlsafe(48)
    db.q("INSERT INTO service_oauth_states(state_hash,user_id,provider,expires_at,encrypted_verifier) VALUES(%s,%s,%s,now()+interval '10 minutes',%s)",(digest,uid,provider,G.encrypt(uid,{'provider':provider,'verifier':verifier})),'none')
    return C.PUBLIC_URL.rstrip('/')+'/connections/'+provider+'/connect?state='+state


def _state(state,provider,consume=False):
    if not re.fullmatch(r'[A-Za-z0-9_-]{30,100}',state or '') or provider not in PROVIDERS:raise G.GoogleError('Invalid connection link.')
    digest=hashlib.sha256(state.encode()).hexdigest()
    sql=("UPDATE service_oauth_states SET used=true WHERE state_hash=%s AND provider=%s AND used=false AND expires_at>now() RETURNING user_id,encrypted_verifier" if consume else
         "SELECT user_id,encrypted_verifier FROM service_oauth_states WHERE state_hash=%s AND provider=%s AND used=false AND expires_at>now()")
    row=db.q(sql,(digest,provider),'one')
    if not row:raise G.GoogleError('Link expired or already used. Start again in your private chat.')
    return row


def authorization_url(state,provider):
    if not configured(provider):raise G.GoogleError('Connection unavailable.')
    row=_state(state,provider)
    bound=G.decrypt(row['user_id'],row['encrypted_verifier'])
    if bound.get('provider')!=provider:raise G.GoogleError('State binding failed.')
    challenge=base64.urlsafe_b64encode(hashlib.sha256(bound['verifier'].encode()).digest()).decode().rstrip('=')
    if provider=='workspace':
        return 'https://accounts.google.com/o/oauth2/v2/auth?'+urlencode({'client_id':C.env('GOOGLE_WORKSPACE_CLIENT_ID'),
          'redirect_uri':callback(provider),'response_type':'code','scope':' '.join(GOOGLE_SCOPES),
          'access_type':'offline','prompt':'select_account consent','state':state,'code_challenge':challenge,'code_challenge_method':'S256'})
    # Public-repository/identity read only; no broad repo scope or write capability.
    return 'https://github.com/login/oauth/authorize?'+urlencode({'client_id':C.env('GITHUB_OAUTH_CLIENT_ID'),
        'redirect_uri':callback(provider),'scope':'read:user','state':state,'code_challenge':challenge,'code_challenge_method':'S256'})


def complete(state,code,provider):
    if not configured(provider) or not isinstance(code,str) or len(code)>3000:raise G.GoogleError('Invalid callback.')
    row=_state(state,provider,True);uid=row['user_id']
    bound=G.decrypt(uid,row['encrypted_verifier'])
    if bound.get('provider')!=provider:raise G.GoogleError('State binding failed.')
    verifier=bound['verifier']
    with httpx.Client(timeout=20,follow_redirects=False) as c:
        if provider=='workspace':
            r=c.post('https://oauth2.googleapis.com/token',data={'client_id':C.env('GOOGLE_WORKSPACE_CLIENT_ID'),
              'client_secret':C.env('GOOGLE_WORKSPACE_CLIENT_SECRET'),'code':code,'grant_type':'authorization_code','redirect_uri':callback(provider),'code_verifier':verifier})
            if r.status_code!=200:raise G.GoogleError('Google consent failed.')
            token=r.json()
            if not set(GOOGLE_SCOPES[2:]).issubset(set(token.get('scope','').split())) or not token.get('refresh_token'):
                raise G.GoogleError('Required Workspace permissions not granted.')
            profile=c.get('https://openidconnect.googleapis.com/v1/userinfo',headers={'Authorization':'Bearer '+token['access_token']})
            if profile.status_code!=200 or not profile.json().get('email_verified'):raise G.GoogleError('Could not verify Google identity.')
            identity=profile.json()['email'];token['client_id']=C.env('GOOGLE_WORKSPACE_CLIENT_ID')
        else:
            r=c.post('https://github.com/login/oauth/access_token',headers={'Accept':'application/json'},
              data={'client_id':C.env('GITHUB_OAUTH_CLIENT_ID'),'client_secret':C.env('GITHUB_OAUTH_CLIENT_SECRET'),
              'code':code,'redirect_uri':callback(provider),'code_verifier':verifier})
            if r.status_code!=200 or not r.json().get('access_token'):raise G.GoogleError('GitHub consent failed.')
            token=r.json()
            if set(token.get('scope','').split(','))-{'read:user',''}:raise G.GoogleError('Unexpected broad GitHub permissions. Not stored.')
            profile=c.get('https://api.github.com/user',headers={'Authorization':'Bearer '+token['access_token'],'Accept':'application/vnd.github+json'})
            if profile.status_code!=200:raise G.GoogleError('Could not verify GitHub identity.')
            identity=profile.json()['login'];token['client_id']=C.env('GITHUB_OAUTH_CLIENT_ID')
    db.q('''INSERT INTO service_connections(user_id,provider,identity,encrypted_tokens) VALUES(%s,%s,%s,%s)
       ON CONFLICT(user_id,provider) DO UPDATE SET identity=EXCLUDED.identity,encrypted_tokens=EXCLUDED.encrypted_tokens,connected_at=now()''',
       (uid,provider,identity,G.encrypt(uid,{'provider':provider,'tokens':token})),'none')
    return {'user_id':uid,'provider':provider,'identity':identity}


def status(uid,provider):
    return db.q('SELECT identity,connected_at FROM service_connections WHERE user_id=%s AND provider=%s',(uid,provider),'one')


def access(uid,provider):
    if not configured(provider):raise G.GoogleError('Connection unavailable.')
    row=db.q('SELECT encrypted_tokens FROM service_connections WHERE user_id=%s AND provider=%s',(uid,provider),'one')
    if not row:raise G.GoogleError('Connect '+provider+' in your private chat first.')
    bound=G.decrypt(uid,row['encrypted_tokens'])
    if bound.get('provider')!=provider:raise G.GoogleError('Credential provider binding failed.')
    token=bound['tokens']
    cid=C.env('GOOGLE_WORKSPACE_CLIENT_ID' if provider=='workspace' else 'GITHUB_OAUTH_CLIENT_ID')
    if token.get('client_id')!=cid:raise G.GoogleError('OAuth app changed. Reconnect.')
    if provider=='github':return token['access_token']
    with httpx.Client(timeout=20) as c:
        r=c.post('https://oauth2.googleapis.com/token',data={'client_id':cid,'client_secret':C.env('GOOGLE_WORKSPACE_CLIENT_SECRET'),
           'refresh_token':token['refresh_token'],'grant_type':'refresh_token'})
    if r.status_code!=200:raise G.GoogleError('Workspace access expired. Reconnect.')
    return r.json()['access_token']


def disconnect(uid,provider):
    row=db.q('SELECT encrypted_tokens FROM service_connections WHERE user_id=%s AND provider=%s',(uid,provider),'one')
    revoked=False
    if row and provider=='workspace':
        try:
            bound=G.decrypt(uid,row['encrypted_tokens'])
            if bound.get('provider')!=provider:raise G.GoogleError('Credential provider binding failed.')
            token=bound['tokens']
            with httpx.Client(timeout=15) as c:
                r=c.post('https://oauth2.googleapis.com/revoke',data={'token':token.get('refresh_token','')})
            revoked=r.status_code==200
        except Exception:pass
    db.q('DELETE FROM service_connections WHERE user_id=%s AND provider=%s',(uid,provider),'none')
    db.q('DELETE FROM service_oauth_states WHERE user_id=%s AND provider=%s',(uid,provider),'none')
    return revoked
