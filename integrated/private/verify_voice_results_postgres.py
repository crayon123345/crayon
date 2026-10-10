"""Opt-in destructive verification on EMPTY LOCAL synthetic PostgreSQL only."""
import os,sys,types,concurrent.futures,pathlib
if os.environ.get('CRAYON_SCRATCH_DB_CONFIRM')!='empty-local-synthetic-only':raise SystemExit('Scratch acknowledgement required')
url=os.environ.get('CRAYON_VOICE_SCRATCH_DB','')
if not url.startswith(('postgresql://sandbox@127.0.0.1:','postgresql://sandbox@localhost:')):raise SystemExit('Local scratch URL required')
import psycopg
with psycopg.connect(url,autocommit=True) as c:
 if c.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='public'").fetchone()[0]:raise SystemExit('Database must be EMPTY')
 if c.execute('SHOW server_encoding').fetchone()[0]!='UTF8':raise SystemExit('UTF8 required')
os.environ.update(DATABASE_URL=url,CRAYON_WEB_ORIGIN='https://crayon.example',CRAYON_VOICE_CANONICAL_ENABLED='on',CRAYON_VOICE_DISCLOSURE_VERSION='v1',CRAYON_VOICE_ADAPTER_CREDENTIALS='[{"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","scopes":["redeem","revalidate","progress","final"]}]')
sys.path.insert(0,str(pathlib.Path(__file__).parent))
import cr_db as db,cr_voice_tickets as T,cr_voice_results as R,cr_web_auth as A,cr_config as C
C.DATABASE_URL=url
db.init()
for sql in ['CREATE TABLE web_sessions(token_hash TEXT PRIMARY KEY,user_id BIGINT,name TEXT,expires_at TIMESTAMPTZ)','CREATE TABLE web_conversations(user_id BIGINT,id TEXT,title TEXT,PRIMARY KEY(user_id,id))','ALTER TABLE messages ADD COLUMN conversation_id TEXT','ALTER TABLE messages ADD COLUMN work_events JSONB']:db.q(sql,fetch='none')
T.init();R.init();header='Bearer '+'a'*43;owner='Bearer '+'b'*43;origin='https://crayon.example';sh=A.digest('b'*43)
db.q("INSERT INTO web_sessions VALUES(%s,17,'synthetic',now()+interval '1 hour')",(sh,),'none');db.q("INSERT INTO web_conversations VALUES(17,%s,'synthetic')",('c'*43,),'none');db.kv_set('voice_consent_17',{'version':'v1','modes':['live']})
sys.modules['cr_web_email_auth']=types.SimpleNamespace(session_check=lambda h:None)
def lease():
 t=T.issue(owner,origin,{'version':1,'conversation_id':'c'*43,'mode':'live','provider_consent_version':'v1'},'127.0.0.1')
 return T.redeem(header,{'version':1,'ticket':t['ticket'],'observed_origin':origin,'mode':'live'})
l=lease();body={'version':1,'lease_id':l['lease_id'],'request_id':l['request_id'],'seq':2,'type':'done','state':'done','transcript_text':'hello synthetic','answer_text':'synthetic answer'}
R.progress(header,{'version':1,'lease_id':l['lease_id'],'seq':1,'type':'transcript_partial','state':'running','text':'ephemeral'})
def commit(_):return R.final(header,body)
with concurrent.futures.ThreadPoolExecutor(8) as pool:out=list(pool.map(commit,range(8)))
assert sum(not x['duplicate'] for x in out)==1,out
assert db.q('SELECT count(*) AS n FROM messages',fetch='one')['n']==2
assert db.q('SELECT count(*) AS n FROM voice_results',fetch='one')['n']==1
print('REAL_PG_8_FINAL_THREADS_ONE_RECEIPT_TWO_HISTORY_ROWS')
before=db.q('SELECT expires_at FROM voice_results',fetch='one')['expires_at']
r=R.recover(owner,origin,l['request_id']);assert all(x['type']!='transcript_partial' for x in r['events'])
assert db.q('SELECT expires_at FROM voice_results',fetch='one')['expires_at']==before
print('REAL_PG_RECOVERY_READONLY_NO_PARTIALS_FIXED_TTL')
try:R.final(header,{**body,'answer_text':'changed'});raise AssertionError('conflict accepted')
except T.TicketError as e:assert e.code=='final_conflict'
T.revoke(owner,origin,{'version':1,'request_id':l['request_id']})
try:R.final(header,body);raise AssertionError('revoked duplicate accepted')
except T.TicketError as e:assert e.code=='revoked'
print('REAL_PG_REVOKED_DUPLICATE_DENIED_AFTER_COMMIT')
db.q('UPDATE voice_results SET expires_at=now()',fetch='none')
try:R.recover(owner,origin,l['request_id']);raise AssertionError('expiry accepted')
except T.TicketError as e:assert e.code=='expired'
l=lease();b={**body,'lease_id':l['lease_id'],'request_id':l['request_id']};original=db.q
count=[0]
def fault(sql,*args,**kw):
 if sql.startswith('INSERT INTO messages'):
  count[0]+=1
  if count[0]==2:raise RuntimeError('synthetic history write failure')
 return original(sql,*args,**kw)
db.q=fault
try:R.final(header,b);raise AssertionError('fault not triggered')
except RuntimeError:pass
finally:db.q=original
assert db.q('SELECT count(*) AS n FROM voice_results WHERE request_id=%s',(l['request_id'],),'one')['n']==0
assert db.q('SELECT count(*) AS n FROM messages',fetch='one')['n']==2
assert db.q('SELECT last_seq FROM voice_leases WHERE request_id=%s',(l['request_id'],),'one')['last_seq']==-1
print('REAL_PG_FINAL_HISTORY_FAILURE_ROLLBACK_ALL')
# Simulated response loss after committed function; exact retry adds nothing.
assert R.final(header,b)['duplicate'] is False
assert R.final(header,b)['duplicate'] is True
assert db.q('SELECT count(*) AS n FROM messages',fetch='one')['n']==4
print('REAL_PG_LOST_RESPONSE_RETRY_ONE_HISTORY_PAIR')
db.q('DELETE FROM web_sessions WHERE token_hash=%s',(sh,),'none')
denied=False
try:R.recover(owner,origin,l['request_id'])
except Exception:denied=True
assert denied,'revoked session accepted'
R.erase_user(17);T.erase_user(17)
assert db.q('SELECT count(*) AS n FROM voice_results',fetch='one')['n']==0
print('REAL_PG_OWNER_ERASURE_ZERO_RESULTS_NO_EXTERNAL_CALLS')
