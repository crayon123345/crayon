"""Destructive synthetic smoke for a DEDICATED EMPTY scratch PostgreSQL only.
Never use an existing/live database. Requires explicit scratch URL and acknowledgement.
"""
import os,sys,types,concurrent.futures
if os.environ.get('CRAYON_SCRATCH_DB_CONFIRM')!='empty-local-synthetic-only':raise SystemExit('Scratch database acknowledgement required')
url=os.environ.get('CRAYON_VOICE_SCRATCH_DB','')
if not url.startswith(('postgresql://sandbox@127.0.0.1:','postgresql://sandbox@localhost:')):raise SystemExit('Local scratch URL required')
os.environ.update(DATABASE_URL=url,CRAYON_WEB_ORIGIN='https://crayon.example',CRAYON_VOICE_CANONICAL_ENABLED='on',CRAYON_VOICE_DISCLOSURE_VERSION='v1',CRAYON_VOICE_ADAPTER_CREDENTIALS='[{"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","scopes":["redeem","revalidate"]}]')
sys.path.insert(0,str(__import__('pathlib').Path(__file__).parent))
import cr_db as db,cr_voice_tickets as V,cr_web_auth as A
import cr_config as C
C.DATABASE_URL=os.environ['DATABASE_URL']
for sql in ['CREATE TABLE IF NOT EXISTS web_sessions(token_hash TEXT PRIMARY KEY,user_id BIGINT,name TEXT,expires_at TIMESTAMPTZ)','CREATE TABLE IF NOT EXISTS web_conversations(user_id BIGINT,id TEXT,title TEXT,PRIMARY KEY(user_id,id))','CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value JSONB)']:db.q(sql,fetch='none')
V.init();sh=A.digest('b'*43);db.q("INSERT INTO web_sessions VALUES(%s,17,'synthetic',now()+interval '1 hour') ON CONFLICT DO NOTHING",(sh,),'none');db.q("INSERT INTO web_conversations VALUES(17,%s,'synthetic') ON CONFLICT DO NOTHING",('c'*43,),'none');db.kv_set('voice_consent_17',{'version':'v1','modes':['live']})
sys.modules['cr_web_email_auth']=types.SimpleNamespace(session_check=lambda h:None)
body={'version':1,'conversation_id':'c'*43,'mode':'live','provider_consent_version':'v1'}
r=V.issue('Bearer '+'b'*43,'https://crayon.example',body,'127.0.0.1')
def claim(_):
 try:return V.redeem('Bearer '+'a'*43,{'version':1,'ticket':r['ticket'],'observed_origin':'https://crayon.example','mode':'live'})
 except V.TicketError as e:return e.code
with concurrent.futures.ThreadPoolExecutor(8) as pool:out=list(pool.map(claim,range(8)))
assert sum(isinstance(x,dict) for x in out)==1,out
print('REAL_PG_8_THREAD_REDEEM',sum(isinstance(x,dict) for x in out),[x for x in out if isinstance(x,str)])
l=next(x for x in out if isinstance(x,dict));assert V.revalidate('Bearer '+'a'*43,{'version':1,'lease_id':l['lease_id']})['active']
V.revoke('Bearer '+'b'*43,'https://crayon.example',{'version':1,'request_id':r['request_id']})
try:V.revalidate('Bearer '+'a'*43,{'version':1,'lease_id':l['lease_id']});raise AssertionError('revoke failed')
except V.TicketError as e:assert e.code=='revoked'
print('REAL_PG_REVOKE_DENIES')
db.q('DELETE FROM voice_leases',fetch='none');db.q('DELETE FROM voice_tickets',fetch='none')
def issue(_):
 try:return V.issue('Bearer '+'b'*43,'https://crayon.example',body,'127.0.0.1')
 except V.TicketError as e:return e.code
with concurrent.futures.ThreadPoolExecutor(12) as pool:out=list(pool.map(issue,range(12)))
assert sum(isinstance(x,dict) for x in out)==6,out
print('REAL_PG_12_THREAD_ISSUE_LIMIT',sum(isinstance(x,dict) for x in out))
t=next(x for x in out if isinstance(x,dict));db.q("UPDATE voice_tickets SET expires_at=now() WHERE ticket_hash=%s",(A.digest(t['ticket']),),'none')
try:V.redeem('Bearer '+'a'*43,{'version':1,'ticket':t['ticket'],'observed_origin':'https://crayon.example','mode':'live'});raise AssertionError('expired accepted')
except V.TicketError as e:assert e.code=='expired'
print('REAL_PG_EXPIRY_NOW_DENIED')
t=next(x for x in out if isinstance(x,dict) and x['ticket']!=t['ticket']);original=db.q
def fail(sql,*args,**kw):
 if sql.startswith('INSERT INTO voice_leases'):raise RuntimeError('synthetic lease insert failure')
 return original(sql,*args,**kw)
db.q=fail
try:V.redeem('Bearer '+'a'*43,{'version':1,'ticket':t['ticket'],'observed_origin':'https://crayon.example','mode':'live'})
except RuntimeError:pass
finally:db.q=original
assert db.q('SELECT redeemed_at FROM voice_tickets WHERE ticket_hash=%s',(A.digest(t['ticket']),),'one')['redeemed_at'] is None
print('REAL_PG_CLAIM_ROLLBACK_PRESERVED')
V.erase_user(17);assert db.q('SELECT count(*) AS n FROM voice_tickets',fetch='one')['n']==0;assert db.q('SELECT count(*) AS n FROM voice_leases',fetch='one')['n']==0
print('REAL_PG_SCRATCH_ONLY_NO_EXTERNAL_CALLS')
