from unittest.mock import patch
import pytest
import cr_mcp_user as U

def test_consent_required_before_network():
 with patch.object(U,'session') as network:
  with pytest.raises(ValueError):U.add(1,'server','https://example.com/mcp',False)
  network.assert_not_called()
def test_connector_owner_isolation_and_saved_notice():
 store={}
 with patch.object(U,'endpoint'),patch.object(U,'session',return_value={'tools':[{'name':'lookup','inputSchema':{'type':'object','properties':{'topic':{'type':'string'}}}}]}),patch.object(U.db,'kv_get',side_effect=lambda k,d=None:store.get(k,d)),patch.object(U.db,'kv_set',side_effect=lambda k,v:store.update({k:v})):
  U.add(1,'server','https://example.com/mcp',True)
  assert U.connectors(1)['server']['notice']==U.NOTICE
  assert U.connectors(2)=={}
def test_prepare_only_no_call_and_no_secret():
 config={'server':{'url':'https://example.com/mcp','tools':[{'name':'lookup','inputSchema':{'type':'object','properties':{'topic':{'type':'string'}},'required':['topic']}}]}}
 with patch.object(U,'connectors',return_value=config),patch.object(U.G,'encrypt',side_effect=lambda uid,p:p),patch.object(U.db,'kv_set'),patch.object(U,'session') as network:
  r=U.prepare(1,'server','lookup',{'topic':'public weather'})
  assert 'Exact disclosed arguments' in r['text'];network.assert_not_called()
  with pytest.raises(ValueError):U.prepare(1,'server','lookup',{'history':'private'})
def test_cancel_no_network():
 with patch.object(U.db,'q',return_value={'value':'encrypted'}),patch.object(U,'session') as network:
  assert 'No MCP call' in U.confirm(1,'id','cancel')['text'];network.assert_not_called()
def test_protocol_modern_discover_then_tools_list():
 calls=[]
 class R:
  headers={'content-type':'application/json'};content=b'{}'
  def __init__(self,obj):self.obj=obj
  def raise_for_status(self):pass
  def json(self):return self.obj
 class Client:
  def __init__(self,**kw):pass
  def __enter__(self):return self
  def __exit__(self,*a):pass
  def post(self,url,headers,json):
   calls.append(json);return R({'id':json['id'],'result':{'tools':[]}})
 with patch.object(U,'endpoint'),patch.object(U.httpx,'Client',Client):
  assert U.session('https://example.com/mcp','tools/list')=={'tools':[]}
  assert [x['method'] for x in calls]==['server/discover','tools/list']
  assert calls[1]['params']['_meta']['io.modelcontextprotocol/protocolVersion']=='2026-07-28'

def test_review_tool_available_in_web_and_no_ambient_disclosure():
 import cr_channel as C,cr_tools as T
 assert 'mcp_review_tool' in C.WEB_TOOLS
 ctx={'uid':1,'chat_id':1,'channel_name':'web','meta':{'user_text':'Use server lookup for Delhi'}}
 with patch.object(U,'prepare',return_value={'review_id':'r','text':'review'}) as prepare:
  assert T.mcp_review_tool(ctx,'server','lookup','{"topic":"Delhi"}')['review_prepared']
  assert ctx['meta']['mcp_reviews'][0]['review_id']=='r'
  assert not T.mcp_review_tool(ctx,'server','lookup','{"topic":"private memory not in request"}')['ok']
  assert prepare.call_count==1


import contextlib as _cl,json
def _enter(*cms):
 stack=_cl.ExitStack()
 for cm in cms:stack.enter_context(cm)
 return stack

# --- mcp wave 1 review-#3 fold: deny-default, true CAS, commit
# visibility, key+value scrub, concurrency controls ------------------------

_GH_URL='https://api.githubcopilot.com/mcp/readonly'
_TOK='ghp_te"st\\+to/ken='
_BLOB={'token':'ghp_x_validtoken1','env':'CRAYON_MCP_GITHUB_TOKEN','url':_GH_URL,'name':'github','gen':1,'rev':1,'scope':'server'}
_ENV={'CRAYON_MCP_GITHUB_TOKEN':_TOK,'CRAYON_MCP_KEYED_UIDS':'1'}

class _FakeDB:
 '''kv table with JSONB semantics: insert-only claims, conditional
 updates and conditional deletes behave like the real SQL.'''
 def __init__(s):
  s.store={};s.audits=[];s.order=[];s.fail_closing_audit=False
 def get(s,k,d=None):return s.store.get(k,d)
 def set(s,k,v):s.store[k]=v
 def q(s,sql,p=(),f=None):
  s.order.append(('audit' if 'INSERT INTO audit' in sql else 'db',sql[:24]))
  if 'INSERT INTO audit' in sql:
   if s.fail_closing_audit and 'begin' not in str(p):raise RuntimeError('audit down')
   s.audits.append(str(p));return None
  if sql.startswith('INSERT INTO kv'):
   key=p[0];val=json.loads(p[1])
   if 'DO UPDATE' in sql:
    if key in s.store:
     oldv=json.loads(p[3])
     if s.store.get(key)!=oldv:return None
     s.store[key]=json.loads(p[2]);return {'value':json.loads(p[2])}
    s.store[key]=val;return {'value':val}
   if 'DO NOTHING' in sql:
    if key in s.store:return None
    s.store[key]=val;return {'value':val}
   s.store[key]=val;return None
  if sql.startswith('UPDATE kv SET value'):
   new=json.loads(p[0]);key=p[1];old=json.loads(p[2])
   if s.store.get(key)!=old:return None
   s.store[key]=new;return {'value':new}
  if sql.startswith('DELETE FROM kv WHERE key=%s AND value'):
   key=p[0];old=json.loads(p[1])
   if s.store.get(key)==old:s.store.pop(key,None)
   return None
  if sql.startswith('DELETE FROM kv WHERE key IN'):
   for k in p:s.store.pop(k,None)
   return None
  if sql.startswith('DELETE FROM kv WHERE key LIKE'):
   s.store['reviews_revoked']=True;return None
  if sql.startswith('DELETE FROM kv WHERE key=%s'):
   s.store.pop(p[0],None);return None
  return None
 def patches(s):
  return [patch.object(U.db,'kv_get',side_effect=s.get),patch.object(U.db,'kv_set',side_effect=s.set),patch.object(U.db,'q',side_effect=s.q)]

def _crypto():
 return [patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),
         patch.object(U.G,'decrypt',side_effect=lambda uid,b:(b['wrapped'] if isinstance(b,dict) and 'wrapped' in b else b))]

def _review(gen=1,rev=1):
 import time as _time
 return {'server':'github','tool':'get_repo','arguments':{'repo':'a/b'},'url':_GH_URL,'until':_time.time()+60,
         'keyed':True,'cred_rev':rev,'cred_gen':gen,'scope':'server','principal':'server-service-account'}

def _keyed_store(db,blob=None,gen=1,rev=1):
 blob=blob or dict(_BLOB,gen=gen,rev=rev)
 db.store.update({'mcp_connectors_1':{'github':{'url':_GH_URL,'keyed':True,'principal':'server-service-account','tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}},'required':['repo']}}]}},
         'mcp_cred_1_github':blob,'mcp_credmeta_1_github':{'url':_GH_URL,'gen':gen,'rev':rev,'scope':'server','principal':'server-service-account'},'mcp_gen_1_github':{'gen':gen}})

def test_deny_default_unset_and_empty_allowlist():
 for env in ({'CRAYON_MCP_GITHUB_TOKEN':_TOK},{'CRAYON_MCP_GITHUB_TOKEN':_TOK,'CRAYON_MCP_KEYED_UIDS':''}):
  with patch.dict('os.environ',env,clear=True),patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
   with pytest.raises(ValueError):U.add_keyed(999,'github',True)
   with pytest.raises(ValueError):U.add_keyed(1,'github',True)
   network.assert_not_called()

def test_keyed_allowlist_permits_only_listed():
 db=_FakeDB()
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK,'CRAYON_MCP_KEYED_UIDS':'7, 8'},clear=True),patch.object(U,'session') as network,_enter(*(db.patches()+_crypto())):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_registry_rejects_unapproved_connector_before_network():
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'random',True)
  network.assert_not_called()

def test_keyed_connector_fails_closed_without_env_credential():
 with patch.dict('os.environ',{'CRAYON_MCP_KEYED_UIDS':'1'},clear=True),patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_keyed_connector_full_commit_order_and_no_plaintext():
 db=_FakeDB()
 def fake_session(url,method,args=None,token=None):
  db.order.append(('network',method))
  assert url==_GH_URL and token==_TOK
  return {'tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}}}}]}
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',side_effect=fake_session),_enter(*(db.patches()+_crypto())):
  r=U.add_keyed(1,'github',True)
  kinds=[k for k,_ in db.order]
  assert kinds.index('network')>kinds.index('audit'),db.order  # begin-audit BEFORE network
  assert kinds[-1]!='network'
  assert db.store['mcp_cred_1_github']=={'wrapped':{'token':_TOK,'env':'CRAYON_MCP_GITHUB_TOKEN','url':_GH_URL,'name':'github','gen':1,'rev':1,'scope':'server'}}
  assert db.store['mcp_gen_1_github']=={'gen':1}
  assert db.store['mcp_connectors_1']['github']['keyed'] is True
  assert db.store['mcp_connectors_1']['github']['principal']=='server-service-account'
  assert r['name']=='github'
  assert all(_TOK not in a for a in db.audits)

def test_commit_visibility_prepare_refused_until_publish():
 db=_FakeDB();seen={}
 orig_insert=db.q
 def hook(sql,p=(),f=None):
  r=orig_insert(sql,p,f)
  if sql.startswith('INSERT INTO kv') and p[0]=='mcp_cred_1_github':
   # mid-commit: blob written, connector NOT yet published
   with pytest.raises(ValueError):U.prepare(1,'github','get_repo',{'repo':'a/b'})
   seen['refused']=True
  return r
 db.q=hook
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}},'required':['repo']}}]}),_enter(*(db.patches()+_crypto())):
  U.add_keyed(1,'github',True)
  assert seen.get('refused') is True
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})  # works only AFTER commit
  assert 'revision 1, generation 1' in r['text']

def test_closing_audit_failure_rolls_back_nothing_visible():
 db=_FakeDB();db.fail_closing_audit=True
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[]}),_enter(*(db.patches()+_crypto())):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  assert 'mcp_connectors_1' not in db.store or 'github' not in db.store.get('mcp_connectors_1',{})
  assert 'mcp_cred_1_github' not in db.store and 'mcp_credmeta_1_github' not in db.store
  assert db.store['mcp_gen_1_github']=={'gen':1}  # generation never reused

def test_rollback_never_clobbers_a_concurrent_workers_row():
 db=_FakeDB();db.fail_closing_audit=True
 other={'wrapped':{'token':'ghp_otherworker1','env':'CRAYON_MCP_GITHUB_TOKEN','url':_GH_URL,'name':'github','gen':9,'rev':1,'scope':'server'}}
 orig=db.q
 def hook(sql,p=(),f=None):
  r=orig(sql,p,f)
  if sql.startswith('INSERT INTO kv') and p[0]=='mcp_cred_1_github':
   db.store['mcp_cred_1_github']=other  # concurrent worker overwrites before our rollback
  return r
 db.q=hook
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[]}),_enter(*(db.patches()+_crypto())):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  assert db.store['mcp_cred_1_github']==other  # conditional delete left it alone

def test_concurrent_add_generation_claim_is_unique():
 db=_FakeDB()
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[]}),_enter(*(db.patches()+_crypto())):
  U.add_keyed(1,'github',True)
  assert db.store['mcp_gen_1_github']=={'gen':1}
  U.remove(1,'github')
  U.add_keyed(1,'github',True)
  assert db.store['mcp_gen_1_github']=={'gen':2}  # never reset
  with patch.object(U.db,'q',return_value={'value':_review(gen=1)}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')  # gen-1 review dead
   network.assert_not_called()

def test_concurrent_rotate_cas_loser_fails():
 db=_FakeDB();_keyed_store(db)
 enc_a={'wrapped':dict(_BLOB,rev=2,token='ghp_rotated_a')}
 def enc_a_fn(uid,p):return enc_a
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated_a','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),_enter(*(db.patches())),patch.object(U.G,'encrypt',side_effect=enc_a_fn),patch.object(U.G,'decrypt',side_effect=lambda uid,b:(b['wrapped'] if isinstance(b,dict) and 'wrapped' in b else b)):
  db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}  # encrypted blob at rev1
  U.rotate_keyed(1,'github',True)  # rotation A wins: blob now rev2
  # rotation B read the SAME rev1 blob and tries the same conditional update
  ck='mcp_cred_1_github';old_blob={'wrapped':dict(_BLOB)}
  row=U.db.q("UPDATE kv SET value=%s::jsonb WHERE key=%s AND value=%s::jsonb RETURNING value",(json.dumps({'wrapped':dict(_BLOB,rev=2,token='ghp_rotated_b')}),ck,json.dumps(old_blob)),'one')
  assert row is None  # CAS rejects the loser
  assert db.store[ck]==enc_a  # winner's value intact

def test_rotation_invalidates_stale_review():
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated9x','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),_enter(*(db.patches()+_crypto())):
  U.rotate_keyed(1,'github',True)
  assert db.store['mcp_credmeta_1_github']['rev']==2
  with patch.object(U.db,'q',return_value={'value':_review(gen=1,rev=1)}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_tampered_blob_name_or_scope_refused_before_network():
 for bad in (dict(_BLOB,name='evil'),dict(_BLOB,scope='other'),dict(_BLOB,url='https://evil.example/mcp')):
  db=_FakeDB();_keyed_store(db,blob=bad)
  with patch.object(U.db,'q',return_value={'value':_review()}),patch.object(U,'session') as network,_enter(*(db.patches()[:2]+_crypto())):
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_review_card_shows_principal_and_generation():
 db=_FakeDB();_keyed_store(db)
 with patch.dict('os.environ',_ENV,clear=True),_enter(*(db.patches()+_crypto())):
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})
  assert 'revision 1, generation 1' in r['text']
  assert 'server-held service account' in r['text'] and 'NOT as you' in r['text']

def test_replacement_semantics_refuse_silent_swap_public_endpoint():
 db=_FakeDB();_keyed_store(db)
 with patch.dict('os.environ',_ENV,clear=True),_enter(*(db.patches()+_crypto())),patch.object(U,'session',return_value={'tools':[]}) as network,patch.object(U,'endpoint'):
  with pytest.raises(ValueError):U.add(1,'github','https://api.githubcopilot.com/mcp/readonly',True)
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_key_and_value_echo_scrubbed_in_catalog_and_result():
 import base64
 from urllib.parse import quote
 forms=[_TOK,quote(_TOK,safe=''),base64.b64encode(_TOK.encode()).decode(),base64.b64encode(_TOK.encode()).decode().rstrip('='),base64.urlsafe_b64encode(_TOK.encode()).decode(),base64.urlsafe_b64encode(_TOK.encode()).decode().rstrip('=')]
 for echoed in forms:
  db=_FakeDB()
  # token as a schema KEY and as a description VALUE
  echo_tools=[{'name':'get_repo','description':'echo '+echoed,'inputSchema':{'type':'object','properties':{echoed:{'type':'string'},'repo':{'type':'string'}}}}]
  def fake_session(url,method,args=None,token=None,_e=echo_tools):
   if method=='tools/list':return {'tools':_e}
   return {'content':[{'text':'result text'}],'ok':True}
  with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',side_effect=fake_session),_enter(*(db.patches()+_crypto())):
   U.add_keyed(1,'github',True)
   cat=json.dumps(db.store['mcp_connectors_1']['github']['tools'],ensure_ascii=False)
   for form in forms:assert form not in cat
   assert 'get_repo' in cat and 'repo' in cat
   def echo_result(url,method,args=None,token=None,_e=echoed):return {'content':[{'text':'echo '+_e}],_e:'keyed-value','ok':True}
   with patch.object(U.db,'q',return_value={'value':_review()}),patch.object(U,'session',side_effect=echo_result):
    out=U.confirm(1,'id','confirm')
    for form in forms:assert form not in out['text']
    assert 'ok' in out['text']

def test_remove_deletes_credential_meta_and_reviews_keeps_generation():
 db=_FakeDB();db.store['mcp_connectors_1']={'server':{}};db.store['mcp_gen_1_server']={'gen':3}
 with patch.object(U.db,'kv_get',side_effect=db.get),patch.object(U.db,'kv_set',side_effect=db.set),patch.object(U.db,'q',side_effect=db.q):
  assert U.remove(1,'server')=={'removed':True}
  assert db.store.get('reviews_revoked') is True
  assert db.store['mcp_gen_1_server']=={'gen':3}
