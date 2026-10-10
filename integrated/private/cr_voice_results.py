"""Unmounted durable voice result preparation. No provider/tools/effect execution.
All writes lock a lease, validate its live canonical owner/session/conversation,
and commit history + receipt together. Recovery is read-only, original 300s TTL.
"""
import hashlib,json
import cr_db as db
import cr_web_auth as A
import cr_voice_tickets as T
from cr_safety import redact,clean_text,looks_like_secret
SCHEMA='''ALTER TABLE voice_leases ADD COLUMN IF NOT EXISTS last_seq BIGINT NOT NULL DEFAULT -1;
ALTER TABLE voice_leases ADD COLUMN IF NOT EXISTS recovery_events JSONB NOT NULL DEFAULT '[]'::jsonb;
CREATE TABLE IF NOT EXISTS voice_results(
 user_id BIGINT NOT NULL,request_id TEXT NOT NULL,session_hash TEXT NOT NULL,
 conversation_id TEXT NOT NULL,origin TEXT NOT NULL,payload_hash TEXT NOT NULL,
 state TEXT NOT NULL,seq BIGINT NOT NULL,events JSONB NOT NULL,
 received_at TIMESTAMPTZ NOT NULL DEFAULT now(),expires_at TIMESTAMPTZ NOT NULL,
 PRIMARY KEY(user_id,request_id));'''
TYPES={'accepted','progress','transcript_partial','transcript_final','answer_partial','answer_final','review_required','artifact','blocked','done'}
STATES={'queued','running','awaiting_review','blocked','done'}
def init():
 for sql in SCHEMA.split(';'):
  if sql.strip():db.q(sql,fetch='none')
def body_check(body,required,optional=()):
 if not isinstance(body,dict) or not set(required)<=set(body) or set(body)-set(required)-set(optional) or type(body.get('version')) is not int or body['version']!=1:raise T.TicketError('malformed',400)
 if type(body.get('seq')) is not int or not 0<=body['seq']<=9223372036854775807:raise T.TicketError('malformed',400)
 if not isinstance(body.get('lease_id'),str) or not A.PATTERN.fullmatch(body['lease_id']):raise T.TicketError('malformed',400)
 for key in ('text','transcript_text','answer_text','error_code'):
  if key in body and (not isinstance(body[key],str) or len(body[key])>(128 if key=='error_code' else 16000)):raise T.TicketError('size',413)
def locked_live(header,lease,scope):
 ah=T.adapter(header,scope)
 row=db.q('SELECT *,expires_at>now() AS valid FROM voice_leases WHERE lease_hash=%s AND adapter_hash=%s FOR UPDATE',(A.digest(lease),ah),'one')
 if not row:raise T.TicketError('unknown',404)
 if row['revoked']:raise T.TicketError('revoked',410)
 if not row['valid']:raise T.TicketError('expired',410)
 T.live_session(row['user_id'],row['session_hash'])
 # Hold conversation ownership against concurrent deletion through this commit.
 if not db.q('SELECT id FROM web_conversations WHERE user_id=%s AND id=%s FOR SHARE',(row['user_id'],row['conversation_id']),'one'):raise T.TicketError('unknown',404)
 return row
def progress(header,body):
 T.require_enabled();body_check(body,['version','lease_id','seq','type','state'],['text'])
 if body['type'] not in TYPES or body['state'] not in STATES:raise T.TicketError('malformed',400)
 if looks_like_secret(body.get('text','')):raise T.TicketError('sensitive_text',400)
 with db._conn().transaction():
  row=locked_live(header,body['lease_id'],'progress');key=(row['user_id'],row['request_id'])
  if db.q('SELECT request_id FROM voice_results WHERE user_id=%s AND request_id=%s',key,'one'):raise T.TicketError('already_final',409)
  if body['seq']<=row['last_seq']:raise T.TicketError('sequence_conflict',409)
  events=list(row['recovery_events'])
  if body['type'] not in {'transcript_partial','answer_partial'}:events.append({k:v for k,v in body.items() if k!='lease_id'})
  db.q('UPDATE voice_leases SET last_seq=%s,recovery_events=%s::jsonb WHERE lease_hash=%s',(body['seq'],json.dumps(events[-50:]),A.digest(body['lease_id'])),'none')
 return {'accepted':True,'seq':body['seq']}
def final(header,body):
 T.require_enabled();body_check(body,['version','lease_id','request_id','seq','type','state'],['transcript_text','answer_text','error_code'])
 if body['type']!='done' or body['state'] not in {'done','blocked'} or not isinstance(body['request_id'],str) or not A.PATTERN.fullmatch(body['request_id']):raise T.TicketError('malformed',400)
 if any(looks_like_secret(body.get(k,'')) for k in ('transcript_text','answer_text')):raise T.TicketError('sensitive_text',400)
 event={k:v for k,v in body.items() if k!='lease_id'}
 digest=hashlib.sha256(json.dumps(event,sort_keys=True,separators=(',',':')).encode()).hexdigest()
 with db._conn().transaction():
  row=locked_live(header,body['lease_id'],'final')
  if body['request_id']!=row['request_id']:raise T.TicketError('binding',403)
  key=(row['user_id'],row['request_id'])
  prior=db.q('SELECT payload_hash FROM voice_results WHERE user_id=%s AND request_id=%s',key,'one')
  # Liveness checked BEFORE exact duplicate: no bypass on lost-response retry.
  if prior:
   if prior['payload_hash']!=digest:raise T.TicketError('final_conflict',409)
   return {'accepted':True,'duplicate':True,'request_id':row['request_id'],'seq':body['seq']}
  if body['seq']<=row['last_seq']:raise T.TicketError('sequence_conflict',409)
  events=list(row['recovery_events'])+[event]
  db.q("INSERT INTO voice_results(user_id,request_id,session_hash,conversation_id,origin,payload_hash,state,seq,events,expires_at) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,now()+interval '300 seconds')",(*key,row['session_hash'],row['conversation_id'],row['origin'],digest,body['state'],body['seq'],json.dumps(events)),'none')
  # No action receipt or tool invocation comes from adapter text. Existing
  # canonical owner/conversation history receives only terminal text, once.
  for role,field in [('user','transcript_text'),('assistant','answer_text')]:
   if body.get(field):db.q('INSERT INTO messages(user_id,conversation_id,role,content,work_events) VALUES(%s,%s,%s,%s,%s::jsonb)',(row['user_id'],row['conversation_id'],role,redact(clean_text(body[field]))[:6000],'[]'),'none')
  db.q('UPDATE voice_leases SET last_seq=%s,recovery_events=\'[]\'::jsonb WHERE lease_hash=%s',(body['seq'],A.digest(body['lease_id'])),'none')
 return {'accepted':True,'duplicate':False,'request_id':row['request_id'],'seq':body['seq']}
def recover(header,origin,request_id):
 T.require_enabled();T.exact_origin(origin)
 if not isinstance(request_id,str) or not A.PATTERN.fullmatch(request_id):raise T.TicketError('malformed',400)
 user=A.session(header);uid=user['user_id'];sh=A.digest(header[7:])
 with db._conn().transaction():
  T.live_session(uid,sh)
  row=db.q('SELECT *,expires_at>now() AS valid FROM voice_results WHERE user_id=%s AND session_hash=%s AND request_id=%s AND origin=%s',(uid,sh,request_id,origin),'one')
  if not row:raise T.TicketError('unknown',404)
  if not row['valid']:raise T.TicketError('expired',410)
  if not db.q('SELECT id FROM web_conversations WHERE user_id=%s AND id=%s FOR SHARE',(uid,row['conversation_id']),'one'):raise T.TicketError('unknown',404)
 return {'version':1,'request_id':request_id,'conversation_id':row['conversation_id'],'state':row['state'],'events':row['events'],'expires_at':row['expires_at'].timestamp()}
def erase_user(uid):
 db.q('DELETE FROM voice_results WHERE user_id=%s',(uid,),'none')
def cleanup():
 # Explicit preparation only: not scheduled or wired into production.
 with db._conn().transaction():
  db.q('DELETE FROM voice_results WHERE expires_at<=now()',fetch='none')
  db.q('DELETE FROM voice_leases WHERE expires_at<=now()',fetch='none')
  db.q('DELETE FROM voice_tickets WHERE expires_at<=now()',fetch='none')
