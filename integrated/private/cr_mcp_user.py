"""Owner-scoped consented connectors; exact call review, no ambient private context."""
import json,re,secrets,time
from urllib.parse import urlsplit
import httpx
import cr_db as db
import cr_google as G
from cr_safety import looks_like_secret,redact
VERSIONS=['2026-07-28','2025-11-25','2025-06-18','2025-03-26']
NOTICE='Tool arguments go to this server. Its tools can read or change external systems. Crayon sends no ambient history or Google/account content. Every call needs an exact arguments review. Only add a server you trust. No secrets in URLs. OAuth/stdio not supported yet.'
def endpoint(url):
 p=urlsplit(url)
 from cr_web import _safe_host
 if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.port not in (None,443) or not _safe_host(p.hostname):raise ValueError('Use a public HTTPS MCP endpoint, no credentials/query/private hosts.')
 return url
def connectors(uid):return db.kv_get('mcp_connectors_'+str(uid),{}) or {}
def session(url,method,args=None):
 endpoint(url);headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':VERSIONS[0]}
 with httpx.Client(timeout=20,follow_redirects=False) as client:
  def rpc(ident,method,params):
   response=client.post(url,headers=headers,json={'jsonrpc':'2.0','id':ident,'method':method,'params':params} if ident is not None else {'jsonrpc':'2.0','method':method,'params':params})
   response.raise_for_status()
   if ident is None:return {}
   sid=response.headers.get('mcp-session-id')
   if sid:
    if not re.fullmatch(r'[!-~]{1,300}',sid):raise ValueError('Invalid MCP session.')
    headers['Mcp-Session-Id']=sid
   if len(response.content)>150000:raise ValueError('MCP response too large.')
   if response.headers.get('content-type','').startswith('text/event-stream'):
    rows=[json.loads(x[5:].strip()) for x in response.text.splitlines() if x.startswith('data:')]
    obj=next((x for x in rows if x.get('id')==ident),{})
   else:obj=response.json()
   if obj.get('id')!=ident:raise ValueError('MCP response mismatch.')
   if obj.get('error'):raise ValueError('MCP method rejected.')
   return obj.get('result',{})
  meta={'_meta':{'io.modelcontextprotocol/protocolVersion':VERSIONS[0],'io.modelcontextprotocol/clientInfo':{'name':'Crayon','version':'1'},'io.modelcontextprotocol/clientCapabilities':{}}}
  try:
   rpc(1,'server/discover',meta);modern=True
  except (ValueError,httpx.HTTPStatusError):
   modern=False;headers['MCP-Protocol-Version']='2025-11-25'
   started=rpc(1,'initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'Crayon','version':'1'}})
   version=started.get('protocolVersion')
   if version not in VERSIONS[1:]:raise ValueError('Unsupported MCP version.')
   headers['MCP-Protocol-Version']=version;rpc(None,'notifications/initialized',{})
  payload={**(meta if modern else {}),**(args or {})}
  return rpc(2,method,payload)
def add(uid,name,url,accept):
 if accept is not True:raise ValueError(NOTICE)
 if not re.fullmatch('[A-Za-z0-9_-]{1,30}',name):raise ValueError('Use a short connector name.')
 endpoint(url);tools=session(url,'tools/list').get('tools',[])
 if not isinstance(tools,list) or len(tools)>50:raise ValueError('Tool catalog too large.')
 safe=[]
 for t in tools:
  if not isinstance(t,dict) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',t.get('name','')) or not isinstance(t.get('inputSchema'),dict):continue
  if len(json.dumps(t))>12000:continue
  safe.append({k:t[k] for k in ('name','description','inputSchema','annotations') if k in t})
 c=connectors(uid)
 if len(c)>=5 and name not in c:raise ValueError('At most5connectors.')
 c[name]={'url':url,'tools':safe,'consent_at':int(time.time()),'notice':NOTICE};db.kv_set('mcp_connectors_'+str(uid),c)
 db.audit(uid,'mcp_connector_consent',json.dumps({'name':name,'url':url,'notice':NOTICE}))
 return {'name':name,'tools':safe,'notice':NOTICE}
def remove(uid,name):
 c=connectors(uid);c.pop(name,None);db.kv_set('mcp_connectors_'+str(uid),c);return {'removed':True}
def prepare(uid,server,tool,arguments):
 c=connectors(uid).get(server)
 if not c or not any(t['name']==tool for t in c['tools']):raise ValueError('Connect this server/tool first.')
 if re.search(r'(?i)(pay|purchase|checkout|charge|transfer|buy|book|order|subscription)',tool):raise ValueError('Money, order and booking tools are not enabled.')
 if not isinstance(arguments,dict) or len(json.dumps(arguments))>8000 or looks_like_secret(json.dumps(arguments)):raise ValueError('Bounded non-secret arguments only.')
 schema=next(t['inputSchema'] for t in c['tools'] if t['name']==tool)
 # Schema is external data, no remote reference resolution.
 if '$ref' in json.dumps(schema):raise ValueError('Referenced schemas not supported.')
 if set(arguments)-set(schema.get('properties',{})) or any(k not in arguments for k in schema.get('required',[])):raise ValueError('Arguments do not match tool fields.')
 for key,value in arguments.items():
  kind=schema.get('properties',{}).get(key,{}).get('type')
  allowed={'string':str,'number':(int,float),'integer':int,'boolean':bool,'object':dict,'array':list}
  if kind in allowed and not isinstance(value,allowed[kind]):raise ValueError('Argument type mismatch.')
 if re.search(r'(?i)(pay|purchase|checkout|charge|transfer_money|buy|book)',tool):raise ValueError('Money or booking tools are not enabled.')
 ident=secrets.token_hex(12);p={'server':server,'tool':tool,'arguments':arguments,'url':c['url'],'until':time.time()+600}
 db.kv_set('mcp_review_'+str(uid)+'_'+ident,G.encrypt(uid,p))
 return {'review_id':ident,'text':'Call '+server+' / '+tool+'\nDestination: '+c['url']+'\nExact disclosed arguments:\n'+json.dumps(arguments,ensure_ascii=False)+'\nThis may change external systems. No automatic retry. Confirm only if you approve these arguments and effects.'}
def confirm(uid,ident,decision):
 key='mcp_review_'+str(uid)+'_'+ident
 row=db.q('DELETE FROM kv WHERE key=%s RETURNING value',(key,),'one')
 if not row:raise ValueError('Review expired or used.')
 if decision=='cancel':return {'text':'Cancelled. No MCP call made.'}
 if decision!='confirm':raise ValueError('Invalid decision.')
 p=G.decrypt(uid,row['value'])
 if p['until']<time.time() or connectors(uid).get(p['server'],{}).get('url')!=p['url']:raise ValueError('Review expired or connector changed.')
 import cr_progress
 db.audit(uid,'mcp_call_review_confirmed',json.dumps({'server':p['server'],'tool':p['tool'],'destination':p['url']}))
 cr_progress.emit('Calling reviewed MCP tool')
 try:r=session(p['url'],'tools/call',{'name':p['tool'],'arguments':p['arguments']})
 except Exception:raise ValueError('MCP result unconfirmed. Check the server before another call. No retry.') from None
 return {'text':redact(json.dumps(r,ensure_ascii=False))[:12000],'untrusted':True,'note':'Server returned this result; not independent verification of its claims.'}
