import pytest,types
from contextlib import nullcontext
from datetime import datetime,timezone
import cr_voice_results as R
import cr_voice_tickets as T
HEADER='Bearer '+'a'*43
LEASE='l'*43
RID='r'*43
FINAL={'version':1,'lease_id':LEASE,'request_id':RID,'seq':2,'type':'done','state':'done','transcript_text':'hi','answer_text':'hello'}
@pytest.fixture
def setup(monkeypatch):
 monkeypatch.setenv('CRAYON_VOICE_CANONICAL_ENABLED','on');monkeypatch.setenv('CRAYON_WEB_ORIGIN','https://crayon.example')
 monkeypatch.setattr(T,'adapter',lambda h,s:'adapterhash');monkeypatch.setattr(T,'live_session',lambda *a:None);monkeypatch.setattr(R.db,'_conn',lambda:types.SimpleNamespace(transaction=nullcontext));monkeypatch.setattr(R.A,'session',lambda h:{'user_id':17})
 row={'valid':True,'revoked':False,'user_id':17,'request_id':RID,'session_hash':R.A.digest('a'*43),'conversation_id':'c'*43,'origin':'https://crayon.example','last_seq':0,'recovery_events':[],'expires_at':datetime(2026,10,10,tzinfo=timezone.utc),'events':[],'state':'done'}
 calls=[];state={'prior':None}
 def q(sql,p=(),fetch='all'):
  calls.append((sql,p))
  if 'FROM voice_leases' in sql:return dict(row)
  if 'FROM web_conversations' in sql:return {'id':row['conversation_id']}
  if 'SELECT payload_hash' in sql:return state['prior']
  if 'SELECT request_id FROM voice_results' in sql:return state['prior']
  if 'SELECT *' in sql and 'FROM voice_results' in sql:return dict(row)
 monkeypatch.setattr(R.db,'q',q)
 return monkeypatch,row,calls,state
@pytest.mark.parametrize('field,value',[('version',True),('seq',True),('seq',-1),('seq',2**63),('answer_text',[]),('lease_id','bad'),('state','running'),('type','answer_final')])
def test_final_malformed_before_storage(setup,field,value):
 _,row,calls,_=setup
 with pytest.raises(T.TicketError):R.final(HEADER,{**FINAL,field:value})
 assert calls==[]
def test_final_one_atomic_history_and_receipt(setup):
 _,row,calls,_=setup
 assert R.final(HEADER,FINAL)['duplicate'] is False
 assert any('FOR UPDATE' in sql for sql,p in calls)
 assert any('FOR SHARE' in sql for sql,p in calls)
 assert sum(sql.startswith('INSERT INTO messages') for sql,p in calls)==2
 assert sum(sql.startswith('INSERT INTO voice_results') for sql,p in calls)==1
 assert any("now()+interval '300 seconds'" in sql for sql,p in calls)
 assert all(LEASE not in str(p) for sql,p in calls)
def test_live_check_before_duplicate(setup):
 monkey,row,calls,state=setup;state['prior']={'payload_hash':'anything'}
 monkey.setattr(T,'live_session',lambda *a:(_ for _ in ()).throw(T.TicketError('revoked',410)))
 with pytest.raises(T.TicketError,match='revoked'):R.final(HEADER,FINAL)
 assert not any('SELECT payload_hash' in sql for sql,p in calls)
def test_identical_duplicate_no_history_or_ttl_write(setup):
 _,row,calls,state=setup
 event={k:v for k,v in FINAL.items() if k!='lease_id'}
 state['prior']={'payload_hash':R.hashlib.sha256(R.json.dumps(event,sort_keys=True,separators=(',',':')).encode()).hexdigest()}
 assert R.final(HEADER,FINAL)['duplicate'] is True
 assert not any(sql.startswith(('INSERT','UPDATE')) for sql,p in calls)
def test_conflicting_final_denied(setup):
 _,row,calls,state=setup;state['prior']={'payload_hash':'different'}
 with pytest.raises(T.TicketError,match='final_conflict'):R.final(HEADER,FINAL)
 assert not any(sql.startswith(('INSERT','UPDATE')) for sql,p in calls)
def test_progress_terminal_guard(setup):
 _,row,calls,state=setup;state['prior']={'request_id':RID}
 with pytest.raises(T.TicketError,match='already_final'):R.progress(HEADER,{'version':1,'lease_id':LEASE,'seq':3,'type':'progress','state':'running'})
 assert not any(sql.startswith('UPDATE') for sql,p in calls)
def test_partials_not_in_recovery(setup):
 _,row,calls,_=setup
 R.progress(HEADER,{'version':1,'lease_id':LEASE,'seq':1,'type':'transcript_partial','state':'running','text':'not durable'})
 update=next(p for sql,p in calls if sql.startswith('UPDATE'))
 assert R.json.loads(update[1])==[]
def test_final_component_not_terminal(setup):
 _,row,calls,_=setup
 R.progress(HEADER,{'version':1,'lease_id':LEASE,'seq':1,'type':'transcript_final','state':'running','text':'durable component'})
 update=next(p for sql,p in calls if sql.startswith('UPDATE'))
 assert R.json.loads(update[1])[0]['type']=='transcript_final'
 assert not any('INSERT INTO voice_results' in sql or 'INSERT INTO messages' in sql for sql,p in calls)
def test_recovery_reads_only_current_owner_session_exact_origin(setup):
 _,row,calls,_=setup
 result=R.recover(HEADER,'https://crayon.example',RID)
 assert result['expires_at']==1791590400.0 and isinstance(result['expires_at'],float)
 assert not any(sql.startswith(('INSERT','UPDATE','DELETE')) for sql,p in calls)
 assert any(p==(17,R.A.digest('a'*43),RID,'https://crayon.example') for sql,p in calls)
def test_recovery_expiry_exact_denied(setup):
 _,row,calls,_=setup;row['valid']=False
 with pytest.raises(T.TicketError,match='expired'):R.recover(HEADER,'https://crayon.example',RID)
def test_erasure_scoped(setup):
 _,row,calls,_=setup;R.erase_user(17)
 assert calls==[('DELETE FROM voice_results WHERE user_id=%s',(17,))]
def test_extra_effect_fields_rejected(setup):
 _,row,calls,_=setup
 with pytest.raises(T.TicketError,match='malformed'):R.final(HEADER,{**FINAL,'user_id':17,'effect_receipt':'madeup'})
 assert calls==[]
def test_disabled(setup):
 monkey,row,calls,_=setup;monkey.delenv('CRAYON_VOICE_CANONICAL_ENABLED')
 with pytest.raises(T.TicketError,match='disabled'):R.final(HEADER,FINAL)
 assert calls==[]
def test_no_adapter_effect_receipt_in_history(setup):
 _,row,calls,_=setup;row['recovery_events']=[{'type':'artifact','text':'unverified adapter claim'}]
 R.final(HEADER,FINAL)
 assert all(p[-1]=='[]' for sql,p in calls if sql.startswith('INSERT INTO messages'))
def test_secret_text_not_persisted(setup):
 _,row,calls,_=setup
 with pytest.raises(T.TicketError,match='sensitive_text'):R.final(HEADER,{**FINAL,'transcript_text':'password=syntheticsecret123'})
 assert not calls
