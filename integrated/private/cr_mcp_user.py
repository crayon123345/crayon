"""Owner-scoped consented connectors; exact call review, no ambient private context."""
import json,re,secrets,time
from urllib.parse import urlsplit
import httpx
import cr_db as db
import cr_google as G
from cr_safety import looks_like_secret,redact
VERSIONS=['2026-07-28','2025-11-25','2025-06-18','2025-03-26']
NOTICE='Tool arguments go to this server. Its tools can read or change external systems. Crayon sends no ambient history or Google/account content. Every call needs an exact arguments review. Only add a server you trust. No secrets in URLs. Bearer credentials come from server environment only, never chat. OAuth/stdio not supported yet.'
# Approved keyed-connector registry: name -> EXACT trusted endpoint, the
# server-env var holding its credential, and the intended policy. Callers
# can never supply an arbitrary endpoint or env var - ownership, intent
# and destination authority come from this registry, not from arguments.
_REGISTRY = {
    'github': {'url': 'https://api.githubcopilot.com/mcp/readonly',
               'env': 'CRAYON_MCP_GITHUB_TOKEN',
               'intent': 'read-only (server-enforced endpoint)'},
    'tavily': {'url': 'https://mcp.tavily.com/mcp/',
               'env': 'CRAYON_MCP_TAVILY_KEY',
               'intent': 'search/extract queries only'},
}
def _scrub(text, token):
    '''EXACT active-token removal: raw, URL-encoded and base64 forms.'''
    if not token or not isinstance(text, str):
        return text
    import base64
    from urllib.parse import quote
    for form in {token, quote(token, safe=''), quote(token, safe='~'),
                 base64.b64encode(token.encode()).decode()}:
        text = text.replace(form, '[redacted-key]')
    return text
def _checked_audit(uid, event, detail):
    '''Audit that GATES the action: db.audit swallows failures, so keyed
    actions use this checked insert - no audit row, no action.'''
    db.q("INSERT INTO audit(user_id,event,detail) VALUES(%s,%s,%s)",
         (uid, event, redact(str(detail))[:500]), "none")
def endpoint(url):
 p=urlsplit(url)
 from cr_web import _safe_host
 if p.scheme!='https' or not p.hostname or p.username or p.password or p.query or p.fragment or p.port not in (None,443) or not _safe_host(p.hostname):raise ValueError('Use a public HTTPS MCP endpoint, no credentials/query/private hosts.')
 return url
def connectors(uid):return db.kv_get('mcp_connectors_'+str(uid),{}) or {}
def session(url,method,args=None,token=None):
 endpoint(url);headers={'Accept':'application/json, text/event-stream','MCP-Protocol-Version':VERSIONS[0]}
 if token is not None:headers['Authorization']='Bearer '+token
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
def add(uid,name,url,accept,token=None):
 if accept is not True:raise ValueError(NOTICE)
 if not re.fullmatch('[A-Za-z0-9_-]{1,30}',name):raise ValueError('Use a short connector name.')
 endpoint(url);tools=session(url,'tools/list',token=token).get('tools',[])
 if not isinstance(tools,list) or len(tools)>50:raise ValueError('Tool catalog too large.')
 safe=[]
 for t in tools:
  if not isinstance(t,dict) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',t.get('name','')) or not isinstance(t.get('inputSchema'),dict):continue
  if len(json.dumps(t))>12000:continue
  safe.append({k:t[k] for k in ('name','description','inputSchema','annotations') if k in t})
 if db.kv_get(_cred_key(uid,name)):raise ValueError('Connector has a stored credential - remove it before replacing.')
 if token:safe=[json.loads(_scrub(json.dumps(t,ensure_ascii=False),token)) for t in safe]
 c=connectors(uid)
 if len(c)>=5 and name not in c:raise ValueError('At most5connectors.')
 c[name]={'url':url,'tools':safe,'consent_at':int(time.time()),'notice':NOTICE};db.kv_set('mcp_connectors_'+str(uid),c)
 db.audit(uid,'mcp_connector_consent',json.dumps({'name':name,'url':url,'notice':NOTICE}))
 return {'name':name,'tools':safe,'notice':NOTICE}
def _cred_key(uid,name):return 'mcp_cred_'+str(uid)+'_'+name
def _cred_meta_key(uid,name):return 'mcp_credmeta_'+str(uid)+'_'+name
def _read_env_credential(env_var):
 if not re.fullmatch(r'[A-Z0-9_]{1,60}',env_var or ''):raise ValueError('Invalid credential variable name.')
 import os
 token=os.environ.get(env_var,'')
 if not re.fullmatch(r'[!-~]{10,300}',token):raise ValueError('Credential missing or unusable in server environment.')
 return token
def _crypto_preflight(uid):
 '''An unwritable/undecryptable credential store must fail BEFORE network.'''
 probe=G.encrypt(uid,{'probe':True})
 if not G.decrypt(uid,probe):raise ValueError('Credential storage unavailable.')
def add_keyed(uid,name,accept):
 '''Registry-approved connector with a server-env bearer credential. The
 endpoint AND the env var come from the approved registry, never from the
 caller. Preflight (registry, env read, crypto round-trip) runs BEFORE any
 network; the credential is stored BEFORE the connector row; the closing
 audit is CHECKED and rolls the connector back on failure.'''
 if accept is not True:raise ValueError(NOTICE)
 entry=_REGISTRY.get(name)
 if not entry:raise ValueError('Connector not in the approved registry.')
 if name in connectors(uid) or db.kv_get(_cred_key(uid,name)):raise ValueError('Connector exists - use rotate_keyed or remove first.')
 token=_read_env_credential(entry['env'])  # fail closed BEFORE any network
 _crypto_preflight(uid)
 url=entry['url'];rev=1
 r=add(uid,name,url,True,token=token)
 db.kv_set(_cred_key(uid,name),G.encrypt(uid,{'token':token,'env':entry['env'],'url':url,'rev':rev,'scope':'server'}))
 db.kv_set(_cred_meta_key(uid,name),{'url':url,'rev':rev,'scope':'server'})
 try:_checked_audit(uid,'mcp_connector_keyed',json.dumps({'name':name,'url':url,'env':entry['env'],'rev':rev}))
 except Exception:
  db.q('DELETE FROM kv WHERE key IN (%s,%s)',(_cred_key(uid,name),_cred_meta_key(uid,name)),'none')
  c=connectors(uid);c.pop(name,None);db.kv_set('mcp_connectors_'+str(uid),c)
  raise ValueError('Audit store unavailable - connector not saved.') from None
 c=connectors(uid);c[name]['keyed']=True;c[name]['intent']=entry['intent'];db.kv_set('mcp_connectors_'+str(uid),c)
 return r
def rotate_keyed(uid,name,accept):
 '''EXPLICIT credential rotation: re-reads the registry env var and bumps
 the revision. Reviews pinned to an older revision are refused at confirm
 - a rotation can never silently swap identity under a pending review.'''
 if accept is not True:raise ValueError(NOTICE)
 entry=_REGISTRY.get(name);c=connectors(uid).get(name)
 if not entry or not c or not c.get('keyed'):raise ValueError('No such keyed connector.')
 token=_read_env_credential(entry['env'])
 _crypto_preflight(uid)
 old_blob=db.kv_get(_cred_key(uid,name));old_meta=db.kv_get(_cred_meta_key(uid,name))
 rev=(old_meta or {}).get('rev',0)+1
 db.kv_set(_cred_key(uid,name),G.encrypt(uid,{'token':token,'env':entry['env'],'url':entry['url'],'rev':rev,'scope':'server'}))
 db.kv_set(_cred_meta_key(uid,name),{'url':entry['url'],'rev':rev,'scope':'server'})
 try:_checked_audit(uid,'mcp_connector_rotated',json.dumps({'name':name,'url':entry['url'],'rev':rev}))
 except Exception:
  if old_blob:db.kv_set(_cred_key(uid,name),old_blob)
  if old_meta:db.kv_set(_cred_meta_key(uid,name),old_meta)
  raise ValueError('Audit store unavailable - rotation not saved.') from None
 return {'name':name,'rev':rev}
def _keyed_credential(uid,name,url):
 '''Fail-closed credential load: missing, corrupt or endpoint-mismatched
 credential = UNAVAILABLE, never an anonymous call.'''
 blob=db.kv_get(_cred_key(uid,name));meta=db.kv_get(_cred_meta_key(uid,name))
 try:
  if not blob or not meta or meta.get('url')!=url:raise ValueError()
  cred=G.decrypt(uid,blob);token=cred['token']
  if cred.get('url')!=url or not re.fullmatch(r'[!-~]{10,300}',token):raise ValueError()
 except Exception:raise ValueError('Credential unavailable - reconnect the connector.') from None
 return token,meta
def remove(uid,name):
 c=connectors(uid);c.pop(name,None);db.kv_set('mcp_connectors_'+str(uid),c)
 db.q('DELETE FROM kv WHERE key IN (%s,%s)',(_cred_key(uid,name),_cred_meta_key(uid,name)),'none');return {'removed':True}
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
 keyed=bool(c.get('keyed'));cred_rev=0
 cred_line='Credential: none (unauthenticated call).'
 if keyed:
  meta=db.kv_get(_cred_meta_key(uid,server))
  if not meta or meta.get('url')!=c['url']:raise ValueError('Credential unavailable - reconnect the connector.')
  cred_rev=meta.get('rev',0)
  cred_line='Credential: server-held key for %s (revision %d, scope: server, endpoint-bound).'%(server,cred_rev)
 ident=secrets.token_hex(12);p={'server':server,'tool':tool,'arguments':arguments,'url':c['url'],'until':time.time()+600,'keyed':keyed,'cred_rev':cred_rev}
 db.kv_set('mcp_review_'+str(uid)+'_'+ident,G.encrypt(uid,p))
 return {'review_id':ident,'text':'Call '+server+' / '+tool+'\nDestination: '+c['url']+'\n'+cred_line+'\nExact disclosed arguments:\n'+json.dumps(arguments,ensure_ascii=False)+'\nThis may change external systems. No automatic retry. Confirm only if you approve these arguments and effects.'}
def confirm(uid,ident,decision):
 key='mcp_review_'+str(uid)+'_'+ident
 row=db.q('DELETE FROM kv WHERE key=%s RETURNING value',(key,),'one')
 if not row:raise ValueError('Review expired or used.')
 if decision=='cancel':return {'text':'Cancelled. No MCP call made.'}
 if decision!='confirm':raise ValueError('Invalid decision.')
 p=G.decrypt(uid,row['value'])
 c=connectors(uid).get(p['server'])
 if p['until']<time.time() or not c or c.get('url')!=p['url']:raise ValueError('Review expired or connector changed.')
 token=None
 if p.get('keyed') or c.get('keyed'):
  meta=db.kv_get(_cred_meta_key(uid,p['server']))
  if not meta or meta.get('rev')!=p.get('cred_rev') or meta.get('url')!=p['url']:raise ValueError('Credential changed since review - ask for a fresh review.')
  token,_=_keyed_credential(uid,p['server'],p['url'])  # BEFORE any network
 import cr_progress
 _checked_audit(uid,'mcp_call_review_confirmed',json.dumps({'server':p['server'],'tool':p['tool'],'destination':p['url'],'cred_rev':p.get('cred_rev',0)}))
 cr_progress.emit('Calling reviewed MCP tool')
 try:r=session(p['url'],'tools/call',{'name':p['tool'],'arguments':p['arguments']},token=token)
 except Exception:raise ValueError(_scrub('MCP result unconfirmed. Check the server before another call. No retry.',token)) from None
 return {'text':_scrub(redact(json.dumps(r,ensure_ascii=False)),token)[:12000],'untrusted':True,'note':'Server returned this result; not independent verification of its claims.'}
