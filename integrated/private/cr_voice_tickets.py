"""Durable canonical voice issue/claim preparation. Not mounted; activation off.
No provider calls, credentials to browser, account reads by adapter or history writes.
"""
import hmac,secrets,json
import cr_db as db
import cr_web_auth as A
import cr_config as C
class TicketError(Exception):
 def __init__(self,code,status):self.code=code;self.status=status;super().__init__(code)
SCHEMA='''CREATE TABLE IF NOT EXISTS voice_tickets(
 ticket_hash TEXT PRIMARY KEY,user_id BIGINT NOT NULL,session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL,origin TEXT NOT NULL,mode TEXT NOT NULL,request_id TEXT UNIQUE NOT NULL,
 issued_at TIMESTAMPTZ NOT NULL DEFAULT now(),expires_at TIMESTAMPTZ NOT NULL,
 redeemed_at TIMESTAMPTZ,revoked BOOLEAN NOT NULL DEFAULT false,ip_hash TEXT NOT NULL);
 CREATE TABLE IF NOT EXISTS voice_leases(
 lease_hash TEXT PRIMARY KEY,user_id BIGINT NOT NULL,session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL,origin TEXT NOT NULL,mode TEXT NOT NULL,request_id TEXT UNIQUE NOT NULL,
 adapter_hash TEXT NOT NULL,redeemed_at TIMESTAMPTZ NOT NULL DEFAULT now(),expires_at TIMESTAMPTZ NOT NULL,
 revoked BOOLEAN NOT NULL DEFAULT false);'''
def init():
 for sql in SCHEMA.split(';'):
  if sql.strip():db.q(sql,fetch='none')
def require_enabled():
 if C.env('CRAYON_VOICE_CANONICAL_ENABLED')!='on':raise TicketError('disabled',503)
def exact_origin(value):
 if not isinstance(value,str) or not hmac.compare_digest(value,A.origin()):raise TicketError('origin',403)
def strict(body,fields):
 if not isinstance(body,dict) or set(body)!=set(fields) or type(body.get('version')) is not int or body.get('version')!=1:raise TicketError('malformed',400)
def live_session(user_id,session_hash):
 row=db.q('SELECT user_id FROM web_sessions WHERE user_id=%s AND token_hash=%s AND expires_at>now() FOR SHARE',(user_id,session_hash),'one')
 if not row:raise TicketError('revoked',410)
 # Includes Firebase disabled/token-revoked checks, fail closed on unavailability.
 __import__('cr_web_email_auth').session_check(session_hash)
def adapter(header,scope):
 # Environment config is server-only JSON [{token,scopes}], not a browser grant.
 try:entries=json.loads(C.env('CRAYON_VOICE_ADAPTER_CREDENTIALS','[]'))
 except Exception:raise TicketError('adapter_config',503) from None
 got=header[7:] if isinstance(header,str) and header.startswith('Bearer ') else ''
 for entry in entries:
  token=entry.get('token','')
  if len(token)>=32 and got and hmac.compare_digest(token,got):
   if scope not in entry.get('scopes',[]):raise TicketError('scope',403)
   return A.digest(token)
 raise TicketError('unauthenticated',401)
def issue(header,origin,body,ip):
 require_enabled();exact_origin(origin)
 strict(body,['version','conversation_id','mode','provider_consent_version'])
 if body['mode'] not in {'transcribe','speak','live'}:raise TicketError('mode',400)
 if not isinstance(ip,str) or not ip or len(ip)>100:raise TicketError('malformed',400)
 user=A.session(header);uid=user['user_id'];session_hash=A.digest(header[7:])
 __import__('cr_conversations').require(uid,body['conversation_id'])
 # Caller sending a version is NOT consent: require canonical persisted grant.
 version=C.env('CRAYON_VOICE_DISCLOSURE_VERSION','')
 grant=db.kv_get('voice_consent_'+str(uid),{})
 if not version or body['provider_consent_version']!=version or grant.get('version')!=version or body['mode'] not in grant.get('modes',[]):raise TicketError('consent',403)
 ticket=secrets.token_urlsafe(32);rid=secrets.token_urlsafe(32);ip_hash=A.digest(str(ip))
 with db._conn().transaction():
  # Account/session and IP locks serialize count+insert without process-local races.
  keys=sorted(['voice_issue_'+str(uid)+'_'+session_hash,'voice_ip_'+ip_hash])
  for key in keys:db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(key,),'none')
  live_session(uid,session_hash)
  counts=db.q("SELECT count(*) FILTER (WHERE user_id=%s AND session_hash=%s) AS owner_count,count(*) FILTER (WHERE ip_hash=%s) AS ip_count FROM voice_tickets WHERE issued_at>now()-interval '60 seconds'",(uid,session_hash,ip_hash),'one')
  if counts['owner_count']>=6 or counts['ip_count']>=30:raise TicketError('rate_limit',429)
  row=db.q("INSERT INTO voice_tickets(ticket_hash,user_id,session_hash,conversation_id,origin,mode,request_id,expires_at,ip_hash) VALUES(%s,%s,%s,%s,%s,%s,%s,now()+interval '120 seconds',%s) RETURNING expires_at",(A.digest(ticket),uid,session_hash,body['conversation_id'],origin,body['mode'],rid,ip_hash),'one')
 return {'version':1,'ticket':ticket,'request_id':rid,'conversation_id':body['conversation_id'],'mode':body['mode'],'expires_at':row['expires_at'].timestamp(),'expires_in':120,'audio_limits':{'max_seconds':600,'max_bytes':20000000},'revalidate_seconds':15,'recovery_seconds':300}
def redeem(header,body):
 require_enabled();adapter_hash=adapter(header,'redeem');strict(body,['version','ticket','observed_origin','mode']);exact_origin(body['observed_origin'])
 if not isinstance(body['ticket'],str) or not A.PATTERN.fullmatch(body['ticket']):raise TicketError('malformed',400)
 with db._conn().transaction():
  row=db.q('SELECT *,expires_at>now() AS valid FROM voice_tickets WHERE ticket_hash=%s FOR UPDATE',(A.digest(body['ticket']),),'one')
  if not row:raise TicketError('unknown',404)
  if row['revoked']:raise TicketError('revoked',410)
  if not row['valid']:raise TicketError('expired',410)
  if row['redeemed_at']:raise TicketError('already_redeemed',409)
  if row['origin']!=body['observed_origin'] or row['mode']!=body['mode']:raise TicketError('binding',403)
  live_session(row['user_id'],row['session_hash']);__import__('cr_conversations').require(row['user_id'],row['conversation_id'])
  db.q('UPDATE voice_tickets SET redeemed_at=now() WHERE ticket_hash=%s',(A.digest(body['ticket']),),'none')
  lease=secrets.token_urlsafe(32)
  out=db.q("INSERT INTO voice_leases(lease_hash,user_id,session_hash,conversation_id,origin,mode,request_id,adapter_hash,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,now()+interval '600 seconds') RETURNING expires_at",(A.digest(lease),row['user_id'],row['session_hash'],row['conversation_id'],row['origin'],row['mode'],row['request_id'],adapter_hash),'one')
 return {'version':1,'lease_id':lease,'request_id':row['request_id'],'conversation_id':row['conversation_id'],'mode':row['mode'],'origin':row['origin'],'expires_at':out['expires_at'].timestamp(),'revalidate_seconds':15,'audio_limits':{'max_seconds':600,'max_bytes':20000000}}

def revalidate(header,body):
 require_enabled();adapter_hash=adapter(header,'revalidate');strict(body,['version','lease_id'])
 if not isinstance(body['lease_id'],str) or not A.PATTERN.fullmatch(body['lease_id']):raise TicketError('malformed',400)
 row=db.q('SELECT *,expires_at>now() AS valid FROM voice_leases WHERE lease_hash=%s AND adapter_hash=%s',(A.digest(body['lease_id']),adapter_hash),'one')
 if not row:raise TicketError('unknown',404)
 if row['revoked']:raise TicketError('revoked',410)
 if not row['valid']:raise TicketError('expired',410)
 live_session(row['user_id'],row['session_hash']);__import__('cr_conversations').require(row['user_id'],row['conversation_id'])
 return {'version':1,'active':True,'lease_id':body['lease_id'],'request_id':row['request_id'],'expires_at':row['expires_at'].timestamp(),'next_check_seconds':15}
def revoke(header,origin,body):
 require_enabled();exact_origin(origin);strict(body,['version','request_id']);
 if not isinstance(body['request_id'],str) or not A.PATTERN.fullmatch(body['request_id']):raise TicketError('malformed',400)
 user=A.session(header);uid=user['user_id'];sh=A.digest(header[7:])
 with db._conn().transaction():
  row=db.q('UPDATE voice_tickets SET revoked=true WHERE user_id=%s AND session_hash=%s AND request_id=%s RETURNING request_id',(uid,sh,body['request_id']),'one')
  if not row:raise TicketError('unknown',404)
  db.q('UPDATE voice_leases SET revoked=true WHERE user_id=%s AND session_hash=%s AND request_id=%s',(uid,sh,body['request_id']),'none')
 return {'revoked':True}
def erase_user(uid):
 # Future canonical all-account erasure must invoke inside its own transaction.
 db.q('DELETE FROM voice_leases WHERE user_id=%s',(uid,),'none')
 db.q('DELETE FROM voice_tickets WHERE user_id=%s',(uid,),'none')
