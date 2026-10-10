"""Server-side Telegram OIDC and one-use, verifier-bound browser handoff.
No bot/client secrets, provider tokens or long-lived sessions in the Pages app.
"""
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import urlencode

import httpx
import jwt
from cryptography.fernet import Fernet

import cr_config as C
import cr_db as db

ISSUER = 'https://oauth.telegram.org'
PATTERN = re.compile(r'[A-Za-z0-9_-]{40,100}')
SCHEMA = '''
CREATE TABLE IF NOT EXISTS web_login_states(
 state_hash TEXT PRIMARY KEY, cookie_hash TEXT NOT NULL, challenge TEXT NOT NULL,
 encrypted TEXT NOT NULL, expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS web_login_codes(
 code_hash TEXT PRIMARY KEY, challenge TEXT NOT NULL, user_id BIGINT NOT NULL,
 name TEXT NOT NULL DEFAULT '', expires_at TIMESTAMPTZ NOT NULL);
CREATE TABLE IF NOT EXISTS web_sessions(
 token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL, name TEXT NOT NULL DEFAULT '',
 created_at TIMESTAMPTZ DEFAULT now(), expires_at TIMESTAMPTZ NOT NULL);
'''


class AuthError(Exception):
    pass


def origin():
    value = C.env('CRAYON_WEB_ORIGIN', 'https://itsppm76.github.io').rstrip('/')
    from urllib.parse import urlsplit
    p = urlsplit(value)
    if p.scheme != 'https' or not p.hostname or p.path or p.query or p.fragment or p.username or p.port:
        raise AuthError('Web origin is not configured safely.')
    return value


def configured():
    return bool(C.env('TELEGRAM_OIDC_CLIENT_ID') and C.env('TELEGRAM_OIDC_CLIENT_SECRET')
                and C.env('CRAYON_WEB_ENCRYPTION_KEY') and db.available())


def init():
    for stmt in SCHEMA.split(';'):
        if stmt.strip():
            db.q(stmt, fetch='none')


def digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def challenge(value):
    return base64.urlsafe_b64encode(hashlib.sha256(value.encode()).digest()).rstrip(b'=').decode()


def _cipher():
    try:
        return Fernet(C.env('CRAYON_WEB_ENCRYPTION_KEY').encode())
    except Exception:
        raise AuthError('Web login is not configured safely.') from None


def begin(handshake,user=None,header=None):
    if not configured():
        raise AuthError('Telegram web login setup is not active yet.')
    if not isinstance(handshake, str) or not re.fullmatch(r'[A-Za-z0-9_-]{43}', handshake):
        raise AuthError('Invalid login request.')
    origin()
    init()
    state, cookie, verifier, nonce = (secrets.token_urlsafe(32) for _ in range(4))
    payload={'state':state,'verifier':verifier,'nonce':nonce}
    if user:
        if not __import__('cr_accounts').standalone(user['user_id']):raise AuthError('This consolidation path starts from a standalone Google/email account.')
        payload.update(uid=user['user_id'],name=user['name'],session=digest(header[7:]))
    blob = _cipher().encrypt(json.dumps(payload).encode()).decode()
    db.q('DELETE FROM web_login_states WHERE expires_at<now()', fetch='none')
    db.q('INSERT INTO web_login_states(state_hash,cookie_hash,challenge,encrypted,expires_at) VALUES(%s,%s,%s,%s,now()+interval \'5 minutes\')',
         (digest(state), '' if user else digest(cookie), handshake, blob), 'none')
    url = ISSUER + '/auth?' + urlencode({'client_id': C.env('TELEGRAM_OIDC_CLIENT_ID'),
        'redirect_uri': C.PUBLIC_URL.rstrip('/') + '/web/auth/callback', 'response_type': 'code',
        'scope': 'openid profile', 'state': state, 'nonce': nonce, 'code_challenge': challenge(verifier),
        'code_challenge_method': 'S256'})
    return (C.PUBLIC_URL.rstrip('/')+'/web/auth/start?merge_state='+state,'') if user else (url,cookie)


def start_merge(state):
    if C.env('CRAYON_ACCOUNT_MERGE_ENABLED')!='on':raise AuthError('Account consolidation is not enabled yet.')
    if not isinstance(state,str) or not PATTERN.fullmatch(state):raise AuthError('Invalid merge login start.')
    cookie=secrets.token_urlsafe(32)
    row=db.q("UPDATE web_login_states SET cookie_hash=%s WHERE state_hash=%s AND cookie_hash='' AND expires_at>now() RETURNING encrypted",(digest(cookie),digest(state)),'one')
    if not row:raise AuthError('Merge login start expired or already used.')
    data=json.loads(_cipher().decrypt(row['encrypted'].encode()))
    if 'uid' not in data:raise AuthError('Original account binding missing.')
    return ISSUER+'/auth?'+urlencode({'client_id':C.env('TELEGRAM_OIDC_CLIENT_ID'),'redirect_uri':C.PUBLIC_URL.rstrip('/')+'/web/auth/callback','response_type':'code','scope':'openid profile','state':state,'nonce':data['nonce'],'code_challenge':challenge(data['verifier']),'code_challenge_method':'S256'}),cookie


class TelegramJWKClient(jwt.PyJWKClient):
    """Decode provider compression before JSON; keep fixed-origin JWKS and cache."""
    def fetch_data(self):
        try:
            response = httpx.get(ISSUER + '/.well-known/jwks.json', timeout=10,
                                 follow_redirects=False)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict) or not isinstance(payload.get('keys'), list):
                raise ValueError('Invalid JWKS')
            if self.jwk_set_cache is not None:
                self.jwk_set_cache.put(payload)
            self._last_successful_fetch = time.monotonic()
            return payload
        except Exception:
            raise AuthError('Telegram signing keys could not be checked. Start login again.') from None


def validate_id_token(token, nonce):
    # Strict RS256 only. Never trust token-supplied jku/x5u URLs or algorithms.
    client = TelegramJWKClient(ISSUER + '/.well-known/jwks.json', timeout=10)
    key = client.get_signing_key_from_jwt(token).key
    claims = jwt.decode(token, key, algorithms=['RS256'], audience=C.env('TELEGRAM_OIDC_CLIENT_ID'),
                        issuer=ISSUER, options={'require': ['exp', 'iat', 'iss', 'aud', 'sub', 'nonce', 'id']}, leeway=5)
    if not hmac.compare_digest(str(claims['nonce']), nonce):
        raise AuthError('Login nonce did not match.')
    if claims['iat'] > time.time() + 5 or claims['iat'] < time.time() - 300:
        raise AuthError('Login token is too old.')
    uid = claims['id']
    if isinstance(uid, str) and re.fullmatch(r'[1-9][0-9]{0,12}', uid):
        uid = int(uid)
    if type(uid) is not int or not 0 < uid < 10**13:
        raise AuthError('Telegram profile ID missing or invalid.')
    return uid, str(claims.get('name') or '')[:100]


def callback(state, code, cookie):
    if not all(isinstance(x, str) and PATTERN.fullmatch(x) for x in (state, cookie)) or not isinstance(code, str) or not 1 <= len(code) <= 2048:
        raise AuthError('Invalid or expired login.')
    # Atomic consumption also prevents exchange retry after uncertain token response.
    row = db.q('DELETE FROM web_login_states WHERE state_hash=%s AND cookie_hash=%s AND expires_at>now() RETURNING *',
               (digest(state), digest(cookie)), 'one')
    if not row:
        raise AuthError('Login expired or was already used. Start again.')
    data = json.loads(_cipher().decrypt(row['encrypted'].encode()))
    if not hmac.compare_digest(data['state'], state):
        raise AuthError('Login binding failed.')
    r = httpx.post(ISSUER + '/token', auth=(C.env('TELEGRAM_OIDC_CLIENT_ID'), C.env('TELEGRAM_OIDC_CLIENT_SECRET')),
        data={'grant_type': 'authorization_code', 'code': code, 'redirect_uri': C.PUBLIC_URL.rstrip('/') + '/web/auth/callback',
              'client_id': C.env('TELEGRAM_OIDC_CLIENT_ID'), 'code_verifier': data['verifier']}, timeout=20)
    r.raise_for_status()
    uid, name = validate_id_token(r.json()['id_token'], data['nonce'])
    if 'uid' in data:
        if C.env('CRAYON_ACCOUNT_MERGE_ENABLED')!='on':raise AuthError('Consolidation is not enabled. Nothing moved.')
        active=db.q('SELECT user_id FROM web_sessions WHERE token_hash=%s AND user_id=%s AND expires_at>now()',(data['session'],data['uid']),'one')
        if not active:raise AuthError('Original session ended. Start the consolidation review again.')
        # Both IDs are authenticated: existing signed-in session plus verified OIDC ID.
        __import__('cr_account_merge').prepare_verified_review({'user_id':data['uid'],'name':data['name']},None,row['challenge'],uid,name,session_hash=data['session'])
        return 'review'
    handoff = secrets.token_urlsafe(32)
    db.q('DELETE FROM web_login_codes WHERE expires_at<now()', fetch='none')
    db.q('INSERT INTO web_login_codes(code_hash,challenge,user_id,name,expires_at) VALUES(%s,%s,%s,%s,now()+interval \'5 minutes\')',
         (digest(handoff), row['challenge'], uid, name), 'none')
    return handoff


def exchange(code, verifier):
    if not all(isinstance(x, str) and PATTERN.fullmatch(x) for x in (code, verifier)):
        raise AuthError('Invalid login handoff.')
    row = db.q('DELETE FROM web_login_codes WHERE code_hash=%s AND challenge=%s AND expires_at>now() RETURNING user_id,name',
               (digest(code), challenge(verifier)), 'one')
    if not row:
        raise AuthError('Login handoff expired or already used.')
    return _new_session(row)


def poll_login(verifier):
    if not isinstance(verifier, str) or not PATTERN.fullmatch(verifier):
        raise AuthError('Invalid login verifier.')
    row = db.q('DELETE FROM web_login_codes WHERE challenge=%s AND expires_at>now() RETURNING user_id,name',
               (challenge(verifier),), 'one')
    return _new_session(row) if row else {'pending': True}


def _new_session(row):
    token = secrets.token_urlsafe(32)
    db.q('DELETE FROM web_sessions WHERE expires_at<now()', fetch='none')
    db.q('INSERT INTO web_sessions(token_hash,user_id,name,expires_at) VALUES(%s,%s,%s,now()+interval \'7 days\')',
         (digest(token), row['user_id'], row['name']), 'none')
    db.audit(row['user_id'], 'web_login')
    return {'token': token, 'expires_in': 604800, 'user': {'id': row['user_id'], 'name': row['name']}}


def session(header):
    if not isinstance(header, str) or not header.startswith('Bearer ') or not PATTERN.fullmatch(header[7:]):
        raise AuthError('Sign in to Crayon first.')
    row = db.q('SELECT user_id,name,expires_at FROM web_sessions WHERE token_hash=%s AND expires_at>now()', (digest(header[7:]),), 'one')
    if not row:
        raise AuthError('Your web session expired. Log in again.')
    __import__('cr_web_email_auth').session_check(digest(header[7:]))
    return row


def logout(header):
    if isinstance(header, str) and header.startswith('Bearer '):
        db.q('DELETE FROM web_sessions WHERE token_hash=%s', (digest(header[7:]),), 'none')
        __import__('cr_web_email_auth').init()
        db.q('DELETE FROM web_email_sessions WHERE token_hash=%s',(digest(header[7:]),),'none')
