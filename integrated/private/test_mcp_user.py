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
  if sql.startswith('WITH ok AS'):
   if s.store.get(p[0])!=json.loads(p[1]):return None
   if s.store.get(p[2])!=json.loads(p[3]):return None
   cc=p[4];newv=json.loads(p[5]);oldv=json.loads(p[7])
   if cc in s.store and s.store[cc]!=oldv:return None
   s.store[cc]=newv;return {'value':newv}
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
   if 'AND (SELECT' in sql and s.store.get(p[3])!=json.loads(p[4]):return None
   if s.store.get(key)!=old:return None
   s.store[key]=new;return {'value':new}
  if sql.startswith('DELETE FROM kv WHERE key=%s AND value'):
   key=p[0];old=json.loads(p[1])
   if s.store.get(key)==old:s.store.pop(key,None)
   return None
  if sql.startswith('DELETE FROM kv WHERE key IN'):
   if 'AND (SELECT' in sql:
    if s.store.get(p[3])!=json.loads(p[4]):return None
    keys=p[:3]
   else:keys=p
   for k in keys:s.store.pop(k,None)
   return None
  if sql.startswith('DELETE FROM kv WHERE key LIKE'):
   if 'AND (SELECT' in sql:
    if s.store.get(p[1])!=json.loads(p[2]):return None
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
  U.remove(1,'github')  # fence bump: gen 1 -> 2
  U.add_keyed(1,'github',True)  # claims gen 3
  assert db.store['mcp_gen_1_github']=={'gen':3}  # never reset, never reused
  with patch.object(U.db,'q',return_value={'value':_review(gen=2)}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')  # gen-2 review dead
   network.assert_not_called()

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
  assert db.store['mcp_gen_1_server']=={'gen':4}  # remove bumps the fence; generation kept monotonic

def _decrypt_wrapped(uid,b):return b['wrapped'] if isinstance(b,dict) and 'wrapped' in b else b

def _unkeyed_review(server='notes',tool='get_note',args=None,url='https://mcp.example/notes'):
 import time as _time
 return {'server':server,'tool':tool,'arguments':args or {'id':'n1'},'url':url,'until':_time.time()+60,'keyed':False}

def test_concurrent_rotate_pending_claim_loser_fails():
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated_a','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),_enter(*(db.patches())),patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),patch.object(U.G,'decrypt',side_effect=_decrypt_wrapped):
  import time as _t
  db.store['mcp_credpending_1_github']={'enc':{'wrapped':dict(_BLOB,rev=2)},'at':int(_t.time()),'by':'x'}  # FRESH in-flight claim
  with pytest.raises(ValueError):U.rotate_keyed(1,'github',True)
  assert db.store['mcp_cred_1_github']=={'wrapped':dict(_BLOB)}  # live blob untouched

def test_concurrent_rotate_stale_read_loses_commit_cas():
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 orig_get=db.get;state={'done':False}
 def hook_get(k,d=None):
  v=orig_get(k,d)
  if k=='mcp_cred_1_github' and not state['done']:
   state['done']=True
   with patch.object(U.db,'kv_get',side_effect=orig_get):
    U.rotate_keyed(1,'github',True)  # rotation A runs fully while B holds a stale read
  return v
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated_WIN','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),patch.object(U.db,'kv_get',side_effect=hook_get),patch.object(U.db,'kv_set',side_effect=db.set),patch.object(U.db,'q',side_effect=db.q),patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),patch.object(U.G,'decrypt',side_effect=_decrypt_wrapped):
  with pytest.raises(ValueError):U.rotate_keyed(1,'github',True)  # B's commit CAS loses
  assert db.store['mcp_cred_1_github']=={'wrapped':dict(_BLOB,rev=2,token='ghp_rotated_WIN')}
  assert 'mcp_credpending_1_github' not in db.store  # loser's pending row cleaned up

def test_rotate_audit_failure_leaves_committed_credential_untouched():
 db=_FakeDB();_keyed_store(db);db.fail_closing_audit=True
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated9x','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),_enter(*(db.patches()+_crypto())):
  with pytest.raises(ValueError):U.rotate_keyed(1,'github',True)
  assert db.store['mcp_cred_1_github']=={'wrapped':dict(_BLOB)}  # rev1 never swapped
  assert db.store['mcp_credmeta_1_github']['rev']==1
  assert 'mcp_credpending_1_github' not in db.store
  db.fail_closing_audit=False
  # the committed rev1 credential still serves a rev1 review - no gap
  with patch.object(U.db,'q',return_value={'value':_review(gen=1,rev=1)}),patch.object(U,'session',return_value={'content':[{'text':'ok'}],'ok':True}) as network:
   out=U.confirm(1,'id','confirm')
   network.assert_called_once_with(_GH_URL,'tools/call',{'name':'get_repo','arguments':{'repo':'a/b'}},token='ghp_x_validtoken1')
   assert 'ok' in out['text']

def test_rotation_pending_window_prepare_uses_committed_revision():
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 seen={}
 orig=db.q
 def hook(sql,p=(),f=None):
  r=orig(sql,p,f)
  if sql.startswith('INSERT INTO kv') and p and p[0]=='mcp_credpending_1_github':
   # mid-rotation: rev2 exists only as PENDING, not yet audited/committed
   rev=U.prepare(1,'github','get_repo',{'repo':'a/b'})
   assert 'revision 1, generation 1' in rev['text']
   seen['prepared']=True
  return r
 db.q=hook
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotatedNEW1','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),_enter(*(db.patches()+_crypto())):
  U.rotate_keyed(1,'github',True)
  assert seen.get('prepared') is True
  assert db.store['mcp_credmeta_1_github']['rev']==2
  with patch.object(U.db,'q',return_value={'value':_review(gen=1,rev=1)}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')  # rev1 review now stale
   network.assert_not_called()

def test_add_remove_fence_no_false_success_no_resurrection():
 db=_FakeDB()
 orig=db.q
 def hook(sql,p=(),f=None):
  if 'INSERT INTO audit' in sql and 'begin' not in str(p):
   r=orig(sql,p,f)
   U.remove(1,'github')  # remove lands DURING the closing audit, before publish
   return r
  return orig(sql,p,f)
 db.q=hook
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[]}),_enter(*(db.patches()+_crypto())):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)  # no false success
  assert 'github' not in db.store.get('mcp_connectors_1',{})  # nothing published
  assert 'mcp_cred_1_github' not in db.store and 'mcp_credmeta_1_github' not in db.store
  assert db.store['mcp_gen_1_github']=={'gen':2}  # remove bumped the fence; never reused

def test_remove_preserves_same_user_other_connectors():
 db=_FakeDB();_keyed_store(db)
 other=dict(_BLOB,name='tavily',url='https://mcp.tavily.com/mcp',gen=4,rev=2)
 db.store['mcp_connectors_1']['tavily']={'url':'https://mcp.tavily.com/mcp','keyed':True,'principal':'server-service-account','tools':[]}
 db.store['mcp_cred_1_tavily']={'wrapped':other}
 db.store['mcp_credmeta_1_tavily']={'url':'https://mcp.tavily.com/mcp','gen':4,'rev':2,'scope':'server','principal':'server-service-account'}
 db.store['mcp_gen_1_tavily']={'gen':4}
 with _enter(*(db.patches())):
  assert U.remove(1,'github')=={'removed':True}
  assert 'github' not in db.store['mcp_connectors_1'] and 'tavily' in db.store['mcp_connectors_1']
  assert 'mcp_cred_1_github' not in db.store and 'mcp_credmeta_1_github' not in db.store
  assert db.store['mcp_cred_1_tavily']=={'wrapped':other}
  assert db.store['mcp_credmeta_1_tavily']['rev']==2
  assert db.store['mcp_gen_1_tavily']=={'gen':4}
  assert db.store['mcp_gen_1_github']=={'gen':2}  # fence bumped, monotonic

def test_unkeyed_confirm_nested_dict_list_result_passthrough():
 db=_FakeDB()
 db.store['mcp_connectors_1']={'notes':{'url':'https://mcp.example/notes','tools':[{'name':'get_note','inputSchema':{'type':'object','properties':{'id':{'type':'string'}},'required':['id']}}]}}
 result={'content':[{'text':'note body'}],'nested':{'list':[1,{'k':'v'},['x','y']],'dict':{'a':{'b':2}}},'ok':True}
 with _enter(*(db.patches()+_crypto())):
  r=U.prepare(1,'notes','get_note',{'id':'n1'})
  assert 'Credential: none' in r['text']
  with patch.object(U.db,'q',return_value={'value':_unkeyed_review()}),patch.object(U,'session',return_value=result) as network:
   out=U.confirm(1,'id','confirm')
   network.assert_called_once_with('https://mcp.example/notes','tools/call',{'name':'get_note','arguments':{'id':'n1'}},token=None)
   assert 'note body' in out['text'] and 'nested' in out['text'] and '"b": 2' in out['text']

def test_post_commit_meta_failure_reconciled_no_false_failure_no_stuck():
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 state={'failed':False}
 def flaky_set(k,v):
  if k=='mcp_credmeta_1_github' and not state['failed']:
   state['failed']=True;raise RuntimeError('db down')
  db.set(k,v)
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated9x','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),patch.object(U.db,'kv_get',side_effect=db.get),patch.object(U.db,'kv_set',side_effect=flaky_set),patch.object(U.db,'q',side_effect=db.q),patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),patch.object(U.G,'decrypt',side_effect=_decrypt_wrapped):
  r=U.rotate_keyed(1,'github',True)  # committed rev2; meta write failed AFTER commit
  assert r['rev']==2 and r['meta_synced'] is False  # NOT a false failure
  assert db.store['mcp_cred_1_github']=={'wrapped':dict(_BLOB,rev=2,token='ghp_rotated9x')}  # live rev2 kept, never rolled back
  assert 'mcp_credpending_1_github' not in db.store  # pending released in finally
  with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotatedAGAIN1','CRAYON_MCP_KEYED_UIDS':'1'},clear=True):
   r=U.rotate_keyed(1,'github',True)  # next rotation NOT stuck
   assert r['rev']==3 and r['meta_synced'] is True
   assert db.store['mcp_credmeta_1_github']['rev']==3

def test_stale_pending_claim_reclaimed_after_db_down():
 import time as _t
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 db.store['mcp_credpending_1_github']={'enc':{'wrapped':dict(_BLOB,rev=2)},'at':int(_t.time())-3600,'by':'crashed'}
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated9x','CRAYON_MCP_KEYED_UIDS':'1'},clear=True),_enter(*(db.patches())),patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),patch.object(U.G,'decrypt',side_effect=_decrypt_wrapped):
  r=U.rotate_keyed(1,'github',True)  # stale claim reclaimed conditionally
  assert r['rev']==2 and r['meta_synced'] is True
  assert 'mcp_credpending_1_github' not in db.store

def test_remove_fence_never_deletes_a_newer_generation():
 # Peer's repro: A removes and pauses before the credential delete; C
 # re-adds at a NEW generation and succeeds; A resumes - A's deletes are
 # fence-bound to the removed generation and MUST NOT delete C's rows.
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 state={'added':False}
 orig=db.q
 def hook(sql,p=(),f=None):
  if sql.startswith('DELETE FROM kv WHERE key IN') and not state['added']:
   state['added']=True
   U.remove(1,'github')  # B finishes an old remove: gen 2->3, old cred deleted
   U.add_keyed(1,'github',True)  # C re-adds at gen 4 BEFORE A's delete runs
  return orig(sql,p,f)
 db.q=hook
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}},'required':['repo']}}]}),_enter(*(db.patches()+_crypto())):
  assert U.remove(1,'github')=={'removed':True}
  assert 'github' in db.store['mcp_connectors_1']  # C's connector intact
  cred=db.store['mcp_cred_1_github']['wrapped']  # C's credential intact (gens: A fence 1->2, B fence 2->3, C claims 4)
  assert cred['gen']==4 and cred['rev']==1 and cred['token']==_TOK
  assert db.store['mcp_gen_1_github']=={'gen':4}
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})  # actually callable
  assert 'revision 1, generation 4' in r['text']

def test_concurrent_other_connector_race_during_remove():
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 state={'raced':False}
 orig=db.q
 def hook(sql,p=(),f=None):
  if sql.startswith('UPDATE kv SET value') and p[1]=='mcp_connectors_1' and not state['raced']:
   state['raced']=True
   # a concurrent publisher lands another connector mid-remove: CAS loses
   cur=db.store['mcp_connectors_1']
   db.store['mcp_connectors_1']=dict(cur,notes={'url':'https://mcp.example/notes','tools':[]})
   return None
  return orig(sql,p,f)
 db.q=hook
 with _enter(*(db.patches())):
  assert U.remove(1,'github')=={'removed':True}
  c=db.store['mcp_connectors_1']
  assert 'github' not in c and 'notes' in c  # retry re-read preserved the concurrent entry
  assert state['raced'] is True

def test_old_remove_cas_retry_superseded_by_new_generation():
 # Peer's exact interleave: A's first connector UPDATE loses; B completes
 # a full remove; C re-adds at gen4; A's RETRY must see its gen2 fence is
 # gone and stop - never remove C's gen4 entry, never orphan the credential.
 db=_FakeDB();_keyed_store(db)
 db.store['mcp_cred_1_github']={'wrapped':dict(_BLOB)}
 state={'raced':False}
 orig=db.q
 def hook(sql,p=(),f=None):
  if sql.startswith('UPDATE kv SET value') and p[1]=='mcp_connectors_1' and not state['raced']:
   state['raced']=True
   U.remove(1,'github')      # B: fence gen 2->3, entry removed, old cred deleted
   U.add_keyed(1,'github',True)  # C: claims gen 4, credential + publish succeed
   return None               # A's CAS loses
  return orig(sql,p,f)
 db.q=hook
 with patch.dict('os.environ',_ENV,clear=True),patch.object(U,'session',return_value={'tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}},'required':['repo']}}]}),_enter(*(db.patches()+_crypto())):
  r=U.remove(1,'github')
  assert r=={'removed':True,'superseded':True}  # reported no-op per linearization
  assert 'github' in db.store['mcp_connectors_1']  # C's gen4 entry NOT removed by A's retry
  cred=db.store['mcp_cred_1_github']['wrapped']
  assert cred['gen']==4 and cred['token']==_TOK  # no orphaned credential
  assert db.store['mcp_gen_1_github']=={'gen':4}
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})  # gen4 positive: prepare works
  assert 'revision 1, generation 4' in r['text']
  with patch.object(U.db,'q',return_value={'value':_review(gen=4,rev=1)}),patch.object(U,'session',return_value={'content':[{'text':'ok'}],'ok':True}) as network:
   out=U.confirm(1,'id','confirm')  # gen4 call positive
   assert 'ok' in out['text']
