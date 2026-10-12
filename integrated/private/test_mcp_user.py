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

# --- mcp wave 1 review-#2 fold: principal policy, generation-ABA,
# pre-network atomic readiness, encoded echo scrub ------------------------

_GH_URL='https://api.githubcopilot.com/mcp/readonly'
_TOK='ghp_te"st\\+to/ken='
_BLOB={'token':'ghp_x_validtoken1','env':'CRAYON_MCP_GITHUB_TOKEN','url':_GH_URL,'name':'github','gen':1,'rev':1,'scope':'server'}

def _kv(store):
 return (lambda k,d=None:store.get(k,d)),(lambda k,v:store.update({k:v}))

def _review(gen=1,rev=1):
 import time as _time
 return {'server':'github','tool':'get_repo','arguments':{'repo':'a/b'},'url':_GH_URL,'until':_time.time()+60,
         'keyed':True,'cred_rev':rev,'cred_gen':gen,'scope':'server','principal':'server-service-account'}

def _patch_db(store):
 kg,kv=_kv(store)
 def fake_q(s,p2,f=None):
  if 'DELETE FROM kv' in s and 'IN' in s and isinstance(p2,tuple):
   for k in p2:store.pop(k,None)
  return None
 return [patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',side_effect=fake_q)]

def _patch_crypto():
 return [patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),
         patch.object(U.G,'decrypt',side_effect=lambda uid,b:(b['wrapped'] if isinstance(b,dict) and 'wrapped' in b else b))]

def test_registry_rejects_unapproved_connector_before_network():
 with patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'random',True)
  network.assert_not_called()

def test_keyed_connector_fails_closed_without_env_credential():
 with patch.dict('os.environ',{},clear=True),patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_keyed_allowlist_enforced_before_network():
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK,'CRAYON_MCP_KEYED_UIDS':'7, 8'}),patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_keyed_connector_full_commit_and_audit_before_network():
 order=[];store={};audits=[]
 kg,kv=_kv(store)
 def fake_q(s,p2,f=None):
  order.append(('audit' if 'INSERT INTO audit' in s else 'db',s[:24]))
  if 'INSERT INTO audit' in s:audits.append(str(p2))
  return None
 def fake_session(url,method,args=None,token=None):
  order.append(('network',method))
  assert url==_GH_URL and token==_TOK
  return {'tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}}}}]}
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK}),patch.object(U,'session',side_effect=fake_session),patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',side_effect=fake_q),patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b['wrapped']):
  r=U.add_keyed(1,'github',True)
  kinds=[k for k,_ in order]
  assert kinds.index('network')>kinds.index('audit'), order
  assert store['mcp_cred_1_github']=={'wrapped':{'token':_TOK,'env':'CRAYON_MCP_GITHUB_TOKEN','url':_GH_URL,'name':'github','gen':1,'rev':1,'scope':'server'}}
  assert store['mcp_gen_1_github']=={'gen':1}
  assert store['mcp_connectors_1']['github']['keyed'] is True
  assert store['mcp_connectors_1']['github']['principal']=='server-service-account'
  assert r['name']=='github'
  assert all(_TOK not in a for a in audits)

def test_storage_probe_failure_refuses_before_network():
 store={};kg,kv=_kv(store)
 def bad_get(k,d=None):
  if k=='mcp_probe_1':return None
  return store.get(k,d)
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK}),patch.object(U,'session') as network,patch.object(U.db,'kv_get',side_effect=bad_get),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',return_value=None),patch.object(U.G,'encrypt',side_effect=lambda uid,p:p),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_commit_failure_rolls_back_everything():
 store={};kg,kv=_kv(store)
 def fake_q(s,p2,f=None):
  if 'INSERT INTO audit' in s and 'mcp_connector_keyed_begin' not in str(p2):raise RuntimeError('audit down')
  if 'DELETE FROM kv' in s and 'IN' in s and isinstance(p2,tuple):
   for k in p2:store.pop(k,None)
  return None
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK}),patch.object(U,'session',return_value={'tools':[]}),patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',side_effect=fake_q),patch.object(U.G,'encrypt',side_effect=lambda uid,p:p),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  assert 'github' not in store.get('mcp_connectors_1',{})
  assert 'mcp_cred_1_github' not in store and 'mcp_credmeta_1_github' not in store

def test_replacement_semantics_refuse_silent_swap_public_endpoint():
 store={'mcp_connectors_1':{'github':{'url':_GH_URL,'tools':[],'keyed':True}},'mcp_cred_1_github':'blob'}
 kg,kv=_kv(store)
 with patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U,'session',return_value={'tools':[]}) as network,patch.object(U,'endpoint'):
  with pytest.raises(ValueError):U.add(1,'github','https://api.githubcopilot.com/mcp/readonly',True)
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def _keyed_store(blob=None,gen=1,rev=1):
 blob=blob or dict(_BLOB,gen=gen,rev=rev)
 return {'mcp_connectors_1':{'github':{'url':_GH_URL,'keyed':True,'principal':'server-service-account','tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}},'required':['repo']}}]}},
         'mcp_cred_1_github':blob,'mcp_credmeta_1_github':{'url':_GH_URL,'gen':gen,'rev':rev,'scope':'server','principal':'server-service-account'},'mcp_gen_1_github':{'gen':gen}}

def test_review_card_shows_principal_and_generation():
 store=_keyed_store()
 with _enter(_enter(patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK}),*(_patch_db(store)+_patch_crypto()))):
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})
  assert 'revision 1, generation 1' in r['text']
  assert 'server-held service account' in r['text'] and 'NOT as you' in r['text']

def test_generation_aba_old_review_refused_after_delete_readd():
 store=_keyed_store();kg,kv=_kv(store)
 def fake_q(s,p2,f=None):
  if 'DELETE FROM kv' in s and 'IN' in s and isinstance(p2,tuple):
   for k in p2:store.pop(k,None)
  if 'LIKE' in s:store['reviews_revoked']=True
  return None
 base=[patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',side_effect=fake_q),patch.object(U.G,'encrypt',side_effect=lambda uid,p:p),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b)]
 with _enter(patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK}),patch.object(U,'session',return_value={'tools':[]}),*base):
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})
  assert 'generation 1' in r['text']
  U.remove(1,'github')
  assert store.get('reviews_revoked') is True
  assert 'mcp_cred_1_github' not in store
  U.add_keyed(1,'github',True)
  assert store['mcp_gen_1_github']=={'gen':2}
  with patch.object(U.db,'q',return_value={'value':_review(gen=1)}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_tampered_blob_name_or_scope_refused_before_network():
 for bad in (dict(_BLOB,name='evil'),dict(_BLOB,scope='other'),dict(_BLOB,url='https://evil.example/mcp')):
  store=_keyed_store(blob=bad)
  with _enter(_enter(patch.object(U.db,'q',return_value={'value':_review()}),*(_patch_db(store)+_patch_crypto()))) as _st:
   network=_st.enter_context(patch.object(U,'session'))
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_rotation_invalidates_stale_review():
 store=_keyed_store()
 with _enter(patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated9'}),*(_patch_db(store)+_patch_crypto())):
  U.rotate_keyed(1,'github',True)
  assert store['mcp_credmeta_1_github']['rev']==2
  with patch.object(U.db,'q',return_value={'value':_review(gen=1,rev=1)}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_encoded_echo_scrubbed_in_catalog_and_result():
 import base64
 from urllib.parse import quote
 forms=[_TOK,quote(_TOK,safe=''),base64.b64encode(_TOK.encode()).decode(),base64.b64encode(_TOK.encode()).decode().rstrip('='),base64.urlsafe_b64encode(_TOK.encode()).decode(),base64.urlsafe_b64encode(_TOK.encode()).decode().rstrip('=')]
 for echoed in forms:
  store={}
  echo_tools=[{'name':'get_repo','description':'echo '+echoed,'inputSchema':{'type':'object','properties':{'repo':{'type':'string'}}}}]
  def fake_session(url,method,args=None,token=None,_e=echo_tools):
   if method=='tools/list':return {'tools':_e}
   return {'content':[{'text':'result text'}],'ok':True}
  with _enter(patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':_TOK}),patch.object(U,'session',side_effect=fake_session),*(_patch_db(store)+_patch_crypto())):
   U.add_keyed(1,'github',True)
   cat=json.dumps(store['mcp_connectors_1']['github']['tools'],ensure_ascii=False)
   for form in forms:assert form not in cat
   assert 'get_repo' in cat
   def echo_result(url,method,args=None,token=None,_e=echoed):return {'content':[{'text':'echo '+_e}],'ok':True}
   with patch.object(U.db,'q',return_value={'value':_review()}),patch.object(U,'session',side_effect=echo_result):
    out=U.confirm(1,'id','confirm')
    for form in forms:assert form not in out['text']
    assert 'ok' in out['text']

def test_remove_deletes_credential_meta_and_reviews_keeps_generation():
 deleted=[];store={'mcp_connectors_1':{'server':{}},'mcp_gen_1_server':{'gen':3}}
 def fake_q(s,p2,f=None):
  if 'DELETE FROM kv' in s:deleted.append((s[:40],p2))
  return None
 with patch.object(U.db,'kv_get',side_effect=lambda k,d=None:store.get(k,d)),patch.object(U.db,'kv_set',side_effect=lambda k,v:store.update({k:v})),patch.object(U.db,'q',side_effect=fake_q):
  assert U.remove(1,'server')=={'removed':True}
  assert deleted[0][1]==('mcp_cred_1_server','mcp_credmeta_1_server')
  assert 'LIKE' in deleted[1][0] and deleted[1][1]==('mcp_review_1_%',)
  assert store['mcp_gen_1_server']=={'gen':3}
