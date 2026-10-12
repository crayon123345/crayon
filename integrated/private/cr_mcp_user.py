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
# server-env var holding its credential, the intended policy, and the
# PRINCIPAL the call runs as. Callers can never supply an arbitrary
# endpoint, env var, or principal - destination, intent and identity
# authority come from this registry, not from arguments.
_PRINCIPAL = 'server-service-account'
_PRINCIPAL_NOTICE = ("Principal: the app's shared server-held service account. "
                     "The call is made as that service identity, NOT as you, "
                     "and carries no user-level authority.")
_REGISTRY = {
    'github': {'url': 'https://api.githubcopilot.com/mcp/readonly',
               'env': 'CRAYON_MCP_GITHUB_TOKEN',
               'intent': 'read-only (server-enforced endpoint)',
               'principal': _PRINCIPAL},
    'tavily': {'url': 'https://mcp.tavily.com/mcp/',
               'env': 'CRAYON_MCP_TAVILY_KEY',
               'intent': 'search/extract queries only',
               'principal': _PRINCIPAL},
}
def _scrub_forms(token):
    '''Every encoding of the active token we are required to remove:
    raw, URL-encoded (both safe sets), standard base64 (padded and
    unpadded) and URL-safe base64 (padded and unpadded).'''
    import base64
    from urllib.parse import quote
    raw = token.encode()
    b64 = base64.b64encode(raw).decode()
    b64u = base64.urlsafe_b64encode(raw).decode()
    return {token, quote(token, safe=''), quote(token, safe='~'),
            b64, b64.rstrip('='), b64u, b64u.rstrip('=')}
def _scrub(text, token):
    '''EXACT active-token removal across every defined encoding.'''
    if not token or not isinstance(text, str):
        return text
    for form in _scrub_forms(token):
        if form:
            text = text.replace(form, '[redacted-key]')
    return text
def _scrub_obj(obj, token):
    '''Scrub STRING VALUES BEFORE any serialization, so JSON escaping of
    quote/backslash characters cannot smuggle the token past the scrub.
    token=None (unkeyed call) is a PASSTHROUGH: there is no credential to
    remove and the result must be returned intact.'''
    if token is None:
        return obj
    if isinstance(obj, str):
        return _scrub(obj, token)
    if isinstance(obj, list):
        return [_scrub_obj(v, token) for v in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            # A dictionary KEY carrying the credential is DROPPED, not
            # redacted into a collision with a literal "[redacted-key]".
            if isinstance(k, str) and any(f and f in k for f in _scrub_forms(token)):
                continue
            out[k] = _scrub_obj(v, token)
        return out
    return obj
def _checked_audit(uid, event, detail):
    '''Audit that GATES the action: db.audit swallows failures, so keyed
    actions use this checked insert - no audit row, no action.'''
    db.q("INSERT INTO audit(user_id,event,detail) VALUES(%s,%s,%s)",
         (uid, event, redact(str(detail))[:500]), "none")
def _storage_preflight(uid):
    '''Credential/meta storage must be writable AND readable BEFORE any
    network call: an unwritable store discovered after tools/list would
    leave an unkeyed connector behind.'''
    k = 'mcp_probe_' + str(uid)
    db.kv_set(k, {'ok': True})
    if not db.kv_get(k):
        raise ValueError('Credential storage unavailable.')
    db.q('DELETE FROM kv WHERE key=%s', (k,), 'none')
def _principal_allowed(uid, name):
    """Explicit bounded operator policy: keyed connectors run as the
    server service account, spending shared server-held credits.
    CRAYON_MCP_KEYED_UIDS (comma-separated user ids) is the operator's
    grant. UNSET OR EMPTY MEANS DENY FOR EVERY USER - there is no
    default allowance over the shared service identity."""
    import os
    allow = {a.strip() for a in os.environ.get('CRAYON_MCP_KEYED_UIDS', '').split(',') if a.strip()}
    if str(uid) not in allow:
        raise ValueError('Keyed connectors are not enabled for this user.')
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
 if db.kv_get(_cred_key(uid,name)):raise ValueError('Connector has a stored credential - remove it before replacing.')
 endpoint(url);tools=session(url,'tools/list',token=token).get('tools',[])
 if not isinstance(tools,list) or len(tools)>50:raise ValueError('Tool catalog too large.')
 safe=[]
 for t in tools:
  if not isinstance(t,dict) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',t.get('name','')) or not isinstance(t.get('inputSchema'),dict):continue
  if len(json.dumps(t))>12000:continue
  safe.append({k:t[k] for k in ('name','description','inputSchema','annotations') if k in t})
 if token:safe=[_scrub_obj(t,token) for t in safe]
 c=connectors(uid)
 if len(c)>=5 and name not in c:raise ValueError('At most5connectors.')
 c[name]={'url':url,'tools':safe,'consent_at':int(time.time()),'notice':NOTICE};db.kv_set('mcp_connectors_'+str(uid),c)
 db.audit(uid,'mcp_connector_consent',json.dumps({'name':name,'url':url,'notice':NOTICE}))
 return {'name':name,'tools':safe,'notice':NOTICE}
def _cred_key(uid,name):return 'mcp_cred_'+str(uid)+'_'+name
def _cred_meta_key(uid,name):return 'mcp_credmeta_'+str(uid)+'_'+name
def _gen_key(uid,name):return 'mcp_gen_'+str(uid)+'_'+name
def _cred_pending_key(uid,name):return 'mcp_credpending_'+str(uid)+'_'+name
def _read_env_credential(env_var):
 if not re.fullmatch(r'[A-Z0-9_]{1,60}',env_var or ''):raise ValueError('Invalid credential variable name.')
 import os
 token=os.environ.get(env_var,'')
 if not re.fullmatch(r'[!-~]{10,300}',token):raise ValueError('Credential missing or unusable in server environment.')
 return token
def _crypto_preflight(uid):
 probe=G.encrypt(uid,{'probe':True})
 if not G.decrypt(uid,probe):raise ValueError('Credential storage unavailable.')
def _catalog(url,token):
 '''Bounded, sanitized catalog with the active token scrubbed from string
 values BEFORE any serialization.'''
 raw=session(url,'tools/list',token=token).get('tools',[])
 if not isinstance(raw,list) or len(raw)>50:raise ValueError('Tool catalog too large.')
 safe=[]
 for t in raw:
  if not isinstance(t,dict) or not re.fullmatch('[A-Za-z0-9_.-]{1,100}',t.get('name','')) or not isinstance(t.get('inputSchema'),dict):continue
  if len(json.dumps(t))>12000:continue
  safe.append({k:t[k] for k in ('name','description','inputSchema','annotations') if k in t})
 return [_scrub_obj(t,token) for t in safe]
def _rollback_keyed(uid,name,cred_val=None,meta_val=None):
    # CONDITIONAL deletes: only rows still holding exactly what THIS call
    # wrote are removed - a concurrent worker's row is never clobbered.
    # The generation claim is kept: generations are never reused.
    if cred_val is not None:
        db.q('DELETE FROM kv WHERE key=%s AND value=%s::jsonb',(_cred_key(uid,name),json.dumps(cred_val)),'none')
    if meta_val is not None:
        db.q('DELETE FROM kv WHERE key=%s AND value=%s::jsonb',(_cred_meta_key(uid,name),json.dumps(meta_val)),'none')
def _claim_generation(uid,name):
    # Atomic, durable generation claim. The counter survives remove() and
    # is never reused, so a delete/re-add can never resurrect an old review.
    gk=_gen_key(uid,name)
    row=db.q("INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING RETURNING value",(gk,json.dumps({'gen':1})),'one')
    if row:return 1
    cur=(db.kv_get(gk) or {}).get('gen',0)
    row=db.q("UPDATE kv SET value=%s::jsonb WHERE key=%s AND value=%s::jsonb RETURNING value",(json.dumps({'gen':cur+1}),gk,json.dumps({'gen':cur})),'one')
    if not row:raise ValueError('Connector state changed - retry.')
    return cur+1
def add_keyed(uid,name,accept):
    # Registry-approved connector with a server-env bearer credential.
    # Fail-closed, deny-by-default, atomic with commit visibility:
    # registry/principal/env/crypto/storage preflight and a CHECKED
    # begin-audit all run BEFORE any network; the credential and meta are
    # written INSERT-ONLY (a concurrent registration fails instead of
    # clobbering); the CHECKED closing audit lands BEFORE the connector
    # row is published by a CAS update, so the connector is never usable
    # before commit+audit succeed; failures roll back only this call's rows.
    if accept is not True:raise ValueError(NOTICE)
    entry=_REGISTRY.get(name)
    if not entry:raise ValueError('Connector not in the approved registry.')
    if name in connectors(uid) or db.kv_get(_cred_key(uid,name)):raise ValueError('Connector exists - use rotate_keyed or remove first.')
    c=connectors(uid)
    if len(c)>=5:raise ValueError('At most5connectors.')
    _principal_allowed(uid,name)
    token=_read_env_credential(entry['env'])  # fail closed BEFORE any network
    _crypto_preflight(uid)
    _storage_preflight(uid)
    url=entry['url'];gen=_claim_generation(uid,name);rev=1
    _checked_audit(uid,'mcp_connector_keyed_begin',json.dumps({'name':name,'url':url,'env':entry['env'],'gen':gen,'rev':rev}))
    safe=_catalog(url,token)  # network happens only after every gate above
    blob={'token':token,'env':entry['env'],'url':url,'name':name,'gen':gen,'rev':rev,'scope':'server'}
    meta={'url':url,'gen':gen,'rev':rev,'scope':'server','principal':entry['principal']}
    enc=G.encrypt(uid,blob)
    ck=_cred_key(uid,name);mk=_cred_meta_key(uid,name)
    row=db.q("INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING RETURNING value",(ck,json.dumps(enc)),'one')
    if not row:raise ValueError('Connector state changed - retry.')
    try:
        row=db.q("INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING RETURNING value",(mk,json.dumps(meta)),'one')
        if not row:raise ValueError('Connector state changed - retry.')
        _checked_audit(uid,'mcp_connector_keyed',json.dumps({'name':name,'url':url,'env':entry['env'],'gen':gen,'rev':rev}))
        # PUBLISH LAST, by CAS: the connector becomes visible only now.
        cc='mcp_connectors_'+str(uid);old=connectors(uid)
        new=dict(old);new[name]={'url':url,'tools':safe,'keyed':True,'intent':entry['intent'],'principal':entry['principal'],'consent_at':int(time.time()),'notice':NOTICE}
        # Single-statement LIFECYCLE-FENCED publish: the upsert lands only
        # if (a) our generation claim row and (b) our credential row are
        # still EXACTLY what this call wrote, and (c) the connector row
        # still equals what we read. A concurrent remove() bumps the
        # generation and deletes the credential, so either fence fails the
        # whole statement atomically - no published connector without its
        # credential, no false success, no resurrection.
        gj=json.dumps({'gen':gen});cj=json.dumps(enc)
        row=db.q("WITH ok AS (SELECT 1 WHERE (SELECT value FROM kv WHERE key=%s)=%s::jsonb AND (SELECT value FROM kv WHERE key=%s)=%s::jsonb) INSERT INTO kv(key,value) SELECT %s,%s::jsonb FROM ok ON CONFLICT(key) DO UPDATE SET value=%s::jsonb WHERE kv.value=%s::jsonb AND (SELECT value FROM kv WHERE key=%s)=%s::jsonb AND (SELECT value FROM kv WHERE key=%s)=%s::jsonb RETURNING value",(_gen_key(uid,name),gj,ck,cj,cc,json.dumps(new),json.dumps(new),json.dumps(old),_gen_key(uid,name),gj,ck,cj),'one')
        if not row:raise ValueError('Connector state changed - retry.')
    except Exception:
        _rollback_keyed(uid,name,cred_val=enc,meta_val=meta)
        raise ValueError('Credential storage or audit unavailable - connector not saved.') from None
    return {'name':name,'url':url,'tools':[t.get('name') for t in safe],'keyed':True}
def _claim_pending(pk,value):
    # Insert-only pending claim. A FRESH pending row means another rotation
    # is live and we refuse. A STALE row (older than 300s - writer crashed
    # or the DB dropped mid-cleanup) is reclaimed by a conditional update,
    # so a failed cleanup can never wedge rotation permanently.
    row=db.q("INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING RETURNING value",(pk,json.dumps(value)),'one')
    if row:return
    cur=db.kv_get(pk)
    if isinstance(cur,dict) and int(time.time())-int(cur.get('at',0))>300:
        row=db.q("UPDATE kv SET value=%s::jsonb WHERE key=%s AND value=%s::jsonb RETURNING value",(json.dumps(value),pk,json.dumps(cur)),'one')
        if row:return
    raise ValueError('Rotation already in progress - retry.')
def rotate_keyed(uid,name,accept):
    # Explicit rotation with COMMIT VISIBILITY and FINAL-STATE
    # RECONCILIATION. The new credential is staged in a PENDING row
    # (insert-only claim, timestamped so a stale claim is reclaimable),
    # the CHECKED audit lands, and only then is the live blob swapped by a
    # conditional UPDATE. prepare/confirm read ONLY the live blob, so an
    # unaudited revision is never usable. After the swap commits, the
    # derived meta index is reconciled best-effort: a meta write failure
    # does NOT convert a committed rotation into a false failure. OUR
    # pending claim is always released (conditionally) in a finally.
    if accept is not True:raise ValueError(NOTICE)
    entry=_REGISTRY.get(name);c=connectors(uid).get(name)
    if not entry or not c or not c.get('keyed'):raise ValueError('No such keyed connector.')
    _principal_allowed(uid,name)
    token=_read_env_credential(entry['env'])
    _crypto_preflight(uid);_storage_preflight(uid)
    ck=_cred_key(uid,name);pk=_cred_pending_key(uid,name)
    old_blob=db.kv_get(ck)
    try:old=G.decrypt(uid,old_blob)
    except Exception:raise ValueError('Credential unavailable - reconnect the connector.') from None
    gen=old.get('gen',1);rev=old.get('rev',0)+1
    new_enc=G.encrypt(uid,{'token':token,'env':entry['env'],'url':entry['url'],'name':name,'gen':gen,'rev':rev,'scope':'server'})
    pending={'enc':new_enc,'at':int(time.time()),'by':secrets.token_hex(6)}
    _claim_pending(pk,pending)
    try:
        _checked_audit(uid,'mcp_connector_rotated',json.dumps({'name':name,'url':entry['url'],'gen':gen,'rev':rev}))
    except Exception:
        try:db.q('DELETE FROM kv WHERE key=%s AND value=%s::jsonb',(pk,json.dumps(pending)),'none')
        except Exception:pass
        raise ValueError('Audit store unavailable - rotation not saved.') from None
    committed=False;meta_synced=False
    try:
        row=db.q("UPDATE kv SET value=%s::jsonb WHERE key=%s AND value=%s::jsonb RETURNING value",(json.dumps(new_enc),ck,json.dumps(old_blob)),'one')
        if not row:raise ValueError('Credential changed during rotation - retry.')
        committed=True
        # Meta is a DERIVED, non-authoritative index. The live identity
        # change is already committed and may already be in use - never
        # rolled back to hide an error; a meta failure is only reported.
        try:
            db.kv_set(_cred_meta_key(uid,name),{'url':entry['url'],'gen':gen,'rev':rev,'scope':'server','principal':entry['principal']})
            meta_synced=True
        except Exception:
            meta_synced=False
    finally:
        try:db.q('DELETE FROM kv WHERE key=%s AND value=%s::jsonb',(pk,json.dumps(pending)),'none')
        except Exception:pass
    return {'name':name,'rev':rev,'gen':gen,'meta_synced':meta_synced}
def _keyed_credential(uid,name,url):
 '''Fail-closed credential load. The encrypted blob is the AUTHENTICATED
 record: name, endpoint and scope must all match, and a missing or
 corrupt credential is UNAVAILABLE, never an anonymous call.'''
 blob=db.kv_get(_cred_key(uid,name))
 try:
  if not blob:raise ValueError()
  cred=G.decrypt(uid,blob);token=cred['token']
  if cred.get('name')!=name or cred.get('url')!=url or cred.get('scope')!='server' or not re.fullmatch(r'[!-~]{10,300}',token):raise ValueError()
 except Exception:raise ValueError('Credential unavailable - reconnect the connector.') from None
 return token,cred
def remove(uid,name):
    # Removal fence. FIRST claim the removal generation: create the row
    # insert-only if absent, else bump it with a conditional update. Every
    # delete below is bound to THIS fence value, so a re-add that claims a
    # newer generation turns the old remove's deletes into no-ops - an old
    # remove can never delete a new credential, and a mid-flight add
    # always loses its publish fence instead of resurrecting. The entry
    # removal is a conditional update of exactly the row we read.
    gk=_gen_key(uid,name)
    row=db.q("INSERT INTO kv(key,value) VALUES(%s,%s) ON CONFLICT(key) DO NOTHING RETURNING value",(gk,json.dumps({'gen':1})),'one')
    if row:
        fence={'gen':1}
    else:
        g=db.kv_get(gk) or {}
        fence={'gen':g.get('gen',0)+1}
        row=db.q("UPDATE kv SET value=%s::jsonb WHERE key=%s AND value=%s::jsonb RETURNING value",(json.dumps(fence),gk,json.dumps(g)),'one')
        if not row:raise ValueError('Connector state changed - retry.')
    # EVERY entry-removal attempt (including retries) is conditional on the
    # connector row we read AND on OUR fence still holding. If the fence
    # moved, a newer lifecycle operation owns this connector: this remove
    # is superseded - it touches nothing further and reports no-op.
    cc='mcp_connectors_'+str(uid)
    for _ in range(3):
        old=connectors(uid)
        if name not in old:break
        new=dict(old);new.pop(name)
        row=db.q("UPDATE kv SET value=%s::jsonb WHERE key=%s AND value=%s::jsonb AND (SELECT value FROM kv WHERE key=%s)=%s::jsonb RETURNING value",(json.dumps(new),cc,json.dumps(old),gk,json.dumps(fence)),'one')
        if row:break
        if (db.kv_get(gk) or {})!=fence:return {'removed':True,'superseded':True}
    else:
        raise ValueError('Connector state changed - retry.')
    fj=json.dumps(fence)
    # Generation-bound deletes: only land while the fence still holds.
    db.q('DELETE FROM kv WHERE key IN (%s,%s,%s) AND (SELECT value FROM kv WHERE key=%s)=%s::jsonb',(_cred_key(uid,name),_cred_meta_key(uid,name),_cred_pending_key(uid,name),gk,fj),'none')
    # Reviews of the REMOVED generation are revoked; reviews pinned to a
    # NEWER generation (a later add) survive.
    db.q('DELETE FROM kv WHERE key LIKE %s AND (SELECT value FROM kv WHERE key=%s)=%s::jsonb',('mcp_review_'+str(uid)+'_%',gk,fj),'none')
    return {'removed':True}
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
 keyed=bool(c.get('keyed'))
 cred_line='Credential: none (unauthenticated call).'
 extra={'keyed':keyed}
 if keyed:
  _principal_allowed(uid,server)
  _,cred=_keyed_credential(uid,server,c['url'])
  cred_line=('Credential: server-held key for %s (revision %d, generation %d, scope: server, endpoint-bound). %s'
             %(server,cred['rev'],cred['gen'],_PRINCIPAL_NOTICE))
  extra={'keyed':True,'cred_rev':cred['rev'],'cred_gen':cred['gen'],'scope':'server','principal':_PRINCIPAL}
 ident=secrets.token_hex(12);p={'server':server,'tool':tool,'arguments':arguments,'url':c['url'],'until':time.time()+600,**extra}
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
  _principal_allowed(uid,p['server'])
  token,cred=_keyed_credential(uid,p['server'],p['url'])  # BEFORE any network
  if (cred.get('gen')!=p.get('cred_gen') or cred.get('rev')!=p.get('cred_rev')
   or cred.get('scope')!=p.get('scope') or cred.get('name')!=p['server']):
   raise ValueError('Credential changed since review - ask for a fresh review.')
 import cr_progress
 _checked_audit(uid,'mcp_call_review_confirmed',json.dumps({'server':p['server'],'tool':p['tool'],'destination':p['url'],'cred_rev':p.get('cred_rev',0)}))
 cr_progress.emit('Calling reviewed MCP tool')
 try:r=session(p['url'],'tools/call',{'name':p['tool'],'arguments':p['arguments']},token=token)
 except Exception:raise ValueError(_scrub('MCP result unconfirmed. Check the server before another call. No retry.',token)) from None
 r=_scrub_obj(r,token)  # scrub string values BEFORE serialization
 return {'text':_scrub(redact(json.dumps(r,ensure_ascii=False)),token)[:12000],'untrusted':True,'note':'Server returned this result; not independent verification of its claims.'}
