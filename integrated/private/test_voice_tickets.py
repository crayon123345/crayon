import pytest,types,sys
from contextlib import nullcontext
from datetime import datetime,timezone
import cr_voice_tickets as V
@pytest.fixture
def config(monkeypatch):
 monkeypatch.setenv('CRAYON_VOICE_CANONICAL_ENABLED','on');monkeypatch.setenv('CRAYON_WEB_ORIGIN','https://crayon.example');monkeypatch.setenv('CRAYON_VOICE_DISCLOSURE_VERSION','v1');monkeypatch.setenv('CRAYON_VOICE_ADAPTER_CREDENTIALS','[{"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","scopes":["redeem"]}]')
 monkeypatch.setattr(V.A,'session',lambda header:{'user_id':17});monkeypatch.setattr(V.db,'_conn',lambda:types.SimpleNamespace(transaction=nullcontext));monkeypatch.setattr(V.db,'kv_get',lambda *args:{'version':'v1','modes':['live']});monkeypatch.setattr(V,'live_session',lambda *a:None)
 monkeypatch.setitem(sys.modules,'cr_conversations',types.SimpleNamespace(require=lambda *a:None));return monkeypatch
BODY={'version':1,'conversation_id':'c'*43,'mode':'live','provider_consent_version':'v1'}
def test_disabled_before_auth_or_provider(monkeypatch):
 monkeypatch.delenv('CRAYON_VOICE_CANONICAL_ENABLED',raising=False)
 with pytest.raises(V.TicketError,match='disabled'):V.issue('',None,{},'ip')
def test_origin_exact_before_auth(config):
 for origin in [None,'null','https://','https://crayon.example/path','https://evil.example']:
  with pytest.raises(V.TicketError,match='origin'):V.issue('',origin,BODY,'ip')
def test_consent_body_never_grants(config):
 config.setattr(V.db,'kv_get',lambda *a:{})
 with pytest.raises(V.TicketError,match='consent'):V.issue('Bearer '+'b'*43,'https://crayon.example',BODY,'ip')
def test_issue_hash_only_locks_and_server_clock(config):
 calls=[]
 def q(sql,p=(),fetch='all'):
  calls.append((sql,p))
  if 'owner_count' in sql:return {'owner_count':0,'ip_count':0}
  if sql.startswith('INSERT'):return {'expires_at':datetime(2026,10,10,tzinfo=timezone.utc)}
 config.setattr(V.db,'q',q);r=V.issue('Bearer '+'b'*43,'https://crayon.example',BODY,'ip')
 assert r['expires_in']==120 and len(r['ticket'])==43
 assert all(r['ticket'] not in str(p) for sql,p in calls)
 assert sum('advisory_xact_lock' in sql for sql,p in calls)==2
 assert any("now()+interval '120 seconds'" in sql for sql,p in calls)
def test_rate_fails_before_insert(config):
 config.setattr(V.db,'q',lambda sql,p=(),fetch='all':{'owner_count':6,'ip_count':0} if 'owner_count' in sql else None)
 with pytest.raises(V.TicketError,match='rate_limit'):V.issue('Bearer '+'b'*43,'https://crayon.example',BODY,'ip')
def test_adapter_scope(config):
 with pytest.raises(V.TicketError,match='scope'):V.adapter('Bearer '+'a'*43,'progress')
 with pytest.raises(V.TicketError,match='unauthenticated'):V.adapter('Bearer wrong','redeem')
@pytest.mark.parametrize('valid,redeemed,code',[(False,None,'expired'),(True,'y','already_redeemed')])
def test_redeem_expiry_and_once_guard(config,valid,redeemed,code):
 config.setattr(V.db,'q',lambda *a,**k:{'valid':valid,'redeemed_at':redeemed,'revoked':False})
 with pytest.raises(V.TicketError,match=code):V.redeem('Bearer '+'a'*43,{'version':1,'ticket':'b'*43,'observed_origin':'https://crayon.example','mode':'live'})

def test_revalidate_scoped_and_session_live(config):
 config.setenv('CRAYON_VOICE_ADAPTER_CREDENTIALS','[{"token":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","scopes":["revalidate"]}]');calls=[]
 config.setattr(V.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or {'valid':True,'revoked':False,'user_id':17,'session_hash':'hash','conversation_id':'c'*43,'request_id':'req','expires_at':datetime(2026,10,10,tzinfo=timezone.utc)})
 config.setattr(V,'live_session',lambda *a:calls.append(a))
 result=V.revalidate('Bearer '+'a'*43,{'version':1,'lease_id':'b'*43})
 assert result['next_check_seconds']==15 and result['lease_id']=='b'*43 and result['expires_at']==1791590400.0
 assert (17,'hash') in calls
 assert calls[0][1]==(V.A.digest('b'*43),V.A.digest('a'*43))
def test_revocation_scoped_updates(config):
 calls=[];config.setattr(V.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or {'request_id':'req'})
 assert V.revoke('Bearer '+'b'*43,'https://crayon.example',{'version':1,'request_id':'r'*43})['revoked']
 assert all(p==(17,V.A.digest('b'*43),'r'*43) for sql,p in calls)
def test_erasure_exact_owner(config):
 calls=[];config.setattr(V.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)))
 V.erase_user(17);assert all(p==(17,) and 'WHERE user_id=%s' in sql for sql,p in calls)

def test_boolean_version_rejected(config):
 with pytest.raises(V.TicketError,match='malformed'):V.strict({'version':True},['version'])
