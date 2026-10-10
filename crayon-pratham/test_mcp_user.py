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
