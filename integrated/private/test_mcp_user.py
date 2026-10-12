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

# --- mcp wave 1 review-#1 fold: registry ownership, review identity,
# fail-closed keyed state, exact-token echo scrub -------------------------

_GH_URL='https://api.githubcopilot.com/mcp/readonly'

def _kv(store):
 return (lambda k,d=None:store.get(k,d)),(lambda k,v:store.update({k:v}))

def test_registry_rejects_unapproved_connector_before_network():
 with patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'random',True)
  network.assert_not_called()

def test_keyed_connector_fails_closed_without_env_credential():
 with patch.dict('os.environ',{},clear=True),patch.object(U,'session') as network,patch.object(U.db,'kv_get',return_value={}):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_keyed_connector_registry_fixed_endpoint_and_no_plaintext():
 sent={};store={};audits=[]
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
   assert url==_GH_URL  # registry-fixed destination
   sent.update(headers);return R({'id':json['id'],'result':{'protocolVersion':'2026-07-28','tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}}}}]}})
 kg,kv=_kv(store)
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_testtoken123'}),patch.object(U,'endpoint'),patch.object(U.httpx,'Client',Client),patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',side_effect=lambda s,p,f=None:audits.append(str(p)) or None),patch.object(U.G,'encrypt',side_effect=lambda uid,p:{'wrapped':p}),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b['wrapped']):
  r=U.add_keyed(1,'github',True)
  assert sent.get('Authorization')=='Bearer ghp_testtoken123'
  assert store['mcp_cred_1_github']=={'wrapped':{'token':'ghp_testtoken123','env':'CRAYON_MCP_GITHUB_TOKEN','url':_GH_URL,'rev':1,'scope':'server'}}
  assert store['mcp_credmeta_1_github']=={'url':_GH_URL,'rev':1,'scope':'server'}
  assert r['name']=='github' and store['mcp_connectors_1']['github']['keyed'] is True
  assert all('ghp_testtoken123' not in a for a in audits)

def test_replacement_semantics_refuse_silent_swap():
 store={'mcp_connectors_1':{'github':{'url':_GH_URL,'tools':[],'keyed':True}},'mcp_cred_1_github':'blob','mcp_credmeta_1_github':{'url':_GH_URL,'rev':1,'scope':'server'}}
 kg,kv=_kv(store)
 with patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U,'session') as network:
  with pytest.raises(ValueError):U.add(1,'github','https://other.example/mcp',True)
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  network.assert_not_called()

def test_review_binds_credential_revision_and_rotation_invalidates():
 import time as _time
 store={'mcp_connectors_1':{'github':{'url':_GH_URL,'keyed':True,'tools':[{'name':'get_repo','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}},'required':['repo']}}]}},
        'mcp_cred_1_github':'blob','mcp_credmeta_1_github':{'url':_GH_URL,'rev':1,'scope':'server'}}
 kg,kv=_kv(store)
 def enc(uid,p):return p
 def dec(uid,b):return {'token':'ghp_x','url':_GH_URL,'rev':1} if b=='blob' else b
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_rotated9'}),patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',return_value=None),patch.object(U.G,'encrypt',side_effect=enc),patch.object(U.G,'decrypt',side_effect=dec):
  r=U.prepare(1,'github','get_repo',{'repo':'a/b'})
  assert 'revision 1' in r['text']
  U.rotate_keyed(1,'github',True)
  assert store['mcp_credmeta_1_github']['rev']==2
  review={'server':'github','tool':'get_repo','arguments':{'repo':'a/b'},'url':_GH_URL,'until':_time.time()+60,'keyed':True,'cred_rev':1}
  with patch.object(U.db,'q',return_value={'value':review}),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_fail_closed_missing_or_corrupt_credential_before_network():
 import time as _time
 review={'server':'github','tool':'get_repo','arguments':{'repo':'a/b'},'url':_GH_URL,'until':_time.time()+60,'keyed':True,'cred_rev':1}
 base={'mcp_connectors_1':{'github':{'url':_GH_URL,'keyed':True,'tools':[]}},'mcp_credmeta_1_github':{'url':_GH_URL,'rev':1,'scope':'server'}}
 for store in (dict(base),dict(base,**{'mcp_cred_1_github':'corrupt'})):
  kg,kv=_kv(store)
  def dec(uid,blob):
   if blob=='corrupt':raise Exception('bad')
   return blob
  with patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'q',return_value={'value':review}),patch.object(U.G,'decrypt',side_effect=dec),patch.object(U,'session') as network:
   with pytest.raises(ValueError):U.confirm(1,'id','confirm')
   network.assert_not_called()

def test_checked_audit_rolls_back_connector_on_failure():
 store={};kg,kv=_kv(store);deleted=[]
 def fake_q(s,p,f=None):
  if 'INSERT INTO audit' in s:raise RuntimeError('audit down')
  if 'DELETE FROM kv' in s:
   deleted.append(p)
   for k in p:store.pop(k,None)
  return None
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':'ghp_testtoken123'}),patch.object(U,'session',return_value={'tools':[]}),patch.object(U,'endpoint'),patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',side_effect=fake_q),patch.object(U.G,'encrypt',side_effect=lambda uid,p:p),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b):
  with pytest.raises(ValueError):U.add_keyed(1,'github',True)
  assert 'github' not in store.get('mcp_connectors_1',{})
  assert 'mcp_cred_1_github' not in store
  assert deleted

def test_exact_token_echo_scrubbed_in_catalog_result_and_error():
 import base64,time as _time
 from urllib.parse import quote
 tok='ghp_test+token/123'
 enc_forms=[tok,quote(tok,safe=''),base64.b64encode(tok.encode()).decode()]
 store={};kg,kv=_kv(store)
 echo_tools=[{'name':'get_repo','description':'token '+tok+' inside','inputSchema':{'type':'object','properties':{'repo':{'type':'string'}}}}]
 with patch.dict('os.environ',{'CRAYON_MCP_GITHUB_TOKEN':tok}),patch.object(U,'session',return_value={'tools':echo_tools}),patch.object(U,'endpoint'),patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',return_value=None),patch.object(U.G,'encrypt',side_effect=lambda uid,p:p),patch.object(U.G,'decrypt',side_effect=lambda uid,b:b):
  U.add_keyed(1,'github',True)
  cat=str(store['mcp_connectors_1']['github']['tools'])
  for form in enc_forms:assert form not in cat
  assert '[redacted-key]' in cat
 review={'server':'github','tool':'get_repo','arguments':{'repo':'a/b'},'url':_GH_URL,'until':_time.time()+60,'keyed':True,'cred_rev':1}
 store['mcp_cred_1_github']={'token':tok,'url':_GH_URL}
 for echoed in enc_forms:
  def fake_session(url,method,args=None,token=None,_e=echoed):return {'content':[{'text':'echo '+_e}]}
  with patch.object(U.db,'kv_get',side_effect=kg),patch.object(U.db,'kv_set',side_effect=kv),patch.object(U.db,'q',return_value={'value':review}),patch.object(U.G,'decrypt',side_effect=lambda uid,x:x),patch.object(U,'session',side_effect=fake_session):
   out=U.confirm(1,'id','confirm')
   for form in enc_forms:assert form not in out['text']
   assert '[redacted-key]' in out['text']

def test_remove_deletes_credential_and_meta_rows():
 deleted=[]
 with patch.object(U.db,'kv_get',return_value={'server':{}}),patch.object(U.db,'kv_set'),patch.object(U.db,'q',side_effect=lambda s,p,f=None:deleted.append(p) or None):
  assert U.remove(1,'server')=={'removed':True}
  assert deleted==[('mcp_cred_1_server','mcp_credmeta_1_server')]
