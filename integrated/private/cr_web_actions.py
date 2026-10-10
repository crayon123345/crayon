"""Web-only exact review tickets, session-bound and one-use. Google reads bypass AI/history."""
import json,hashlib,secrets
import cr_db as db
import cr_web_auth as A
import cr_google as G

def init():
    db.q("""CREATE TABLE IF NOT EXISTS web_action_reviews(
      id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,session_hash TEXT NOT NULL,
      encrypted TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT DEFAULT 'pending',
      expires_at TIMESTAMPTZ NOT NULL)""",fetch='none')

def connections(uid):
    import cr_connections as X
    return {'google':G.status(uid).get('email'),'workspace':(X.status(uid,'workspace') or {}).get('identity'),
      'github':(X.status(uid,'github') or {}).get('identity'),'calendar_owner':bool(G.status(uid)),'external_auto_send':False,'attachments_email':False}

def preview(uid,header,body):
    init()
    if set(body)!={'kind','fields'} or not isinstance(body['fields'],dict):raise ValueError('Invalid action preview.')
    kind,f=body['kind'],body['fields'];payload={}
    if kind=='workspace_create':
        import cr_workspace_create as C
        if set(f)=={'request'}:
            generated=C.natural_fields(f['request'])
            if not generated:raise ValueError('Ask to create a doc, sheet or slides.')
            f={'type':generated['kind'],'title':generated['title'],'content':generated['content']}
        if set(f)!={'type','title','content'}:raise ValueError('Exact create fields required.')
        d=C.preview(uid,f['type'],f['title'],f['content']);payload=d['payload'];text=d['text']
    elif kind=='form':
        import cr_booking as B
        if set(f)!={'adapter','values'} or not isinstance(f['adapter'],str) or not isinstance(f['values'],dict):raise ValueError('Exact adapter and fields required.')
        d=B.preview(uid,f['adapter'],f['values'])
        payload={'id':d['id'],'hash':d['hash']};text=d['text']
    elif kind=='forget':
        import cr_memory as M,re
        if set(f)!={'key'} or not isinstance(f['key'],str) or not re.fullmatch('[a-z0-9_]{1,100}',f['key']):raise ValueError('Select one exact saved fact key.')
        rows=[x for x in M.facts(uid,100) if x['key']==f['key']]
        if len(rows)!=1:raise ValueError('That saved fact is unavailable. Refresh memory.')
        payload={'key':f['key'],'value':rows[0]['value']}
        text='Forget this exact saved fact?\n'+f['key']+': '+rows[0]['value']+'\nThis removes only this saved fact. Chat history, notes and copies elsewhere remain. It is not all-data deletion.'
    elif kind=='email':
        if set(f)-{'crayon_signature'}!={'to','cc','bcc','subject','body'}:raise ValueError('Exact email fields required.')
        d=G.make_draft(uid,f,structured=True,channel='web')
        # Current draft remains encrypted in provider module. This session ticket gates web sends separately.
        payload={'id':d['id'],'hash':d['hash']}
        text=d['text'].split('\n\nReply with edit/change/rewrite instructions')[0]+'\n\nWeb review: Send exactly this once, or Cancel. No email attachments.'
    elif kind=='calendar':
        import cr_calendar_draft as K
        if set(f)=={'request'}:
            import cr_natural as N
            try:f=N.calendar_fields(f['request'])
            except N.Clarification as e:return {'kind':'clarification','action':'calendar','text':str(e)}
        if set(f)!={'title','start','end','timezone','guests','reminder_minutes'}:raise ValueError('Exact calendar fields required.')
        d=K.preview(uid,f['title'],f['start'],f['end'],f['timezone'],guests=f['guests'],reminder_minutes=f['reminder_minutes'],channel='web')
        payload={'id':d['id'],'hash':d['hash']};text=d['text']
    elif kind=='sheet':
        import cr_workspace as S,cr_connections as X
        if set(f)=={'request'}:
            import cr_natural as N
            try:f=N.sheet_fields(f['request'])
            except N.Clarification as e:return {'kind':'clarification','action':'sheet','text':str(e)}
        if set(f)!={'sid','area','values'}:raise ValueError('Exact sheet fields required.')
        d=S.sheet_preview(uid,f['sid'],f['area'],f['values']);payload=d
        text='Sheet update preview only.\nAccount: '+str((X.status(uid,'workspace') or {}).get('identity'))+'\nSpreadsheet: '+f['sid']+'\nRange: '+f['area']+'\nBefore: '+json.dumps(d['payload']['before'])+'\nWrite as RAW values: '+json.dumps(f['values'])+'\nNo formulas. Compare-before-write and readback. Expires in 10 minutes.'
    else:raise ValueError('Action not enabled.')
    data={'kind':kind,'payload':payload,'text':text};digest=hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest();ident=secrets.token_urlsafe(24)
    db.q("DELETE FROM web_action_reviews WHERE expires_at<now()",fetch='none')
    db.q("INSERT INTO web_action_reviews(id,user_id,session_hash,encrypted,content_hash,expires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')",
      (ident,uid,A.digest(header[7:]),A._cipher().encrypt(json.dumps(data).encode()).decode(),digest),'none')
    return {'review_id':ident,'hash':digest,'text':text,'kind':kind,'expires_in':600,**({'html':d['html'],'fields':d['fields']} if kind=='email' else {})}

def compose_preview(uid,header,body):
    import re,cr_google_chat as H
    from cr_safety import looks_like_secret
    if set(body)!={'message'} or not isinstance(body['message'],str) or not 1<=len(body['message'])<=8000 or looks_like_secret(body['message']):raise ValueError('Invalid compose request.')
    key='web_compose_pending_'+str(uid)+'_'+A.digest(header[7:])
    pending=db.kv_get(key,None)
    saved=G.decrypt(uid,pending) if pending else {}
    if body['message'].strip().lower() in ('cancel','never mind','stop'):
        db.kv_set(key,None);return {'kind':'clarification','text':'Draft preparation cancelled. No email sent.'}
    import time
    text=(saved['request']+'\nAdditional user detail: '+body['message']) if saved.get('until',0)>time.time() else body['message']
    db.kv_set(key,G.encrypt(uid,{'request':text,'until':time.time()+600}))
    parsed=H.classify(text)
    addresses=re.findall(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",text)
    fields={k:parsed.get(k,[]) for k in ('to','cc','bcc')}
    for k in fields:
        if isinstance(fields[k],str):fields[k]=[fields[k]]
        if not isinstance(fields[k],list) or any(x not in addresses for x in fields[k]):raise ValueError('Give exact To/CC/BCC addresses. No recipient guessed.')
    if not fields['to']:
        return {'kind':'clarification','text':"Who should receive it? Give their email address. I kept what you want the email to say."}
    if set(addresses)!=set(fields['to']+fields['cc']+fields['bcc']):raise ValueError('Give exact To/CC/BCC roles for every address.')
    fields.update(subject=parsed.get('subject',''),body=parsed.get('body',''))
    if not fields['subject'] or not fields['body']:return {'kind':'clarification','text':'What should the email say? I kept the recipients.'}
    db.kv_set(key,None)
    return preview(uid,header,{'kind':'email','fields':fields})

def confirm(uid,header,body):
    if set(body)!={'review_id','hash','decision'} or body['decision'] not in ('confirm','cancel') or not isinstance(body['review_id'],str) or not isinstance(body['hash'],str):raise ValueError('Invalid review decision.')
    init()
    row=db.q("UPDATE web_action_reviews SET status='claimed' WHERE id=%s AND user_id=%s AND session_hash=%s AND content_hash=%s AND status='pending' AND expires_at>now() RETURNING encrypted,content_hash",
        (body['review_id'],uid,A.digest(header[7:]),body['hash']),'one')
    if not row:raise ValueError('Review expired, changed, wrong session or already used. No retry.')
    data=json.loads(A._cipher().decrypt(row['encrypted'].encode()))
    if hashlib.sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()!=row['content_hash']:raise ValueError('Review changed. No action made.')
    kind,p=data['kind'],data['payload']
    try:
        if body['decision']=='cancel':
            if kind=='workspace_create':pass
            elif kind=='form':__import__('cr_booking').cancel(uid,p['id'])
            elif kind=='email':G.cancel_draft(uid,p['id'],channel='web')
            elif kind=='calendar':__import__('cr_calendar_draft').cancel(uid,p['id'],channel='web')
            text='Draft discarded. No email sent.' if kind=='email' else 'Cancelled. No external action made.'
        elif kind=='form':
            r=__import__('cr_booking').submit(uid,p['id'],p['hash'])
            text='Form result: '+json.dumps({k:r[k] for k in ('ok','verified','url','note','error') if k in r},ensure_ascii=False)+'\nA controlled form receipt is not proof of a real reservation. If unconfirmed, check the destination before any retry.'
        elif kind=='workspace_create':
            result=__import__('cr_workspace_create').apply(uid,p)
            text=(p['kind'].capitalize()+' created: '+p['title']+'\n'+str(result.get('url') or result.get('id') or '')+'\n'+result['note']) if result.get('created') else result['note']
        elif kind=='email':text=G.send_draft(uid,p['id'],p['hash'],channel='web')
        elif kind=='calendar':text=__import__('cr_calendar_draft').create(uid,p['id'],p['hash'],channel='web')
        elif kind=='forget':
            import cr_memory as M
            with M.user_lock(uid):
                removed=db.q('DELETE FROM facts WHERE user_id=%s AND key=%s AND value=%s RETURNING key',(uid,p['key'],p['value']),'one')
                if not removed:raise ValueError('Saved fact changed or was removed. Refresh and review again.')
                text='Saved fact removed. Chat history and other records were not deleted.'
        elif kind=='sheet':text=json.dumps(__import__('cr_workspace').sheet_apply(uid,p['payload'],p['hash']),ensure_ascii=False)
        else:raise ValueError('Action unavailable.')
        db.q('DELETE FROM web_action_reviews WHERE id=%s AND user_id=%s',(body['review_id'],uid),'none')
        return {'text':text}
    except Exception:
        db.q("UPDATE web_action_reviews SET status='stopped' WHERE id=%s AND user_id=%s",(body['review_id'],uid),'none')
        raise G.GoogleError('Action stopped or uncertain. Check the destination before preparing another request. No automatic retry.') from None

def natural_read_fields(uid,kind,text,session_key=""):
    if not isinstance(text,str) or not 1<=len(text)<=8000:raise ValueError("Ask what you want to read.")
    import re
    if kind=='inbox':
        parsed=__import__('cr_google_chat').classify(text)
        return {'query':parsed.get('query') or 'newer_than:1d'}
    if kind=='email_read':
        m=re.search(r'(?i)\b(first|second|third|fourth|fifth|[1-5])\b',text)
        import time
        saved=db.kv_get('web_mail_results_'+str(uid)+'_'+session_key,{}) or {}
        ids=saved.get('ids',[]) if saved.get('until',0)>time.time() else []
        n={'first':0,'second':1,'third':2,'fourth':3,'fifth':4}.get(m[1].lower(),int(m[1])-1 if m and m[1].isdigit() else -1) if m else -1
        if not 0<=n<len(ids):raise ValueError('Which email? Ask me to show your inbox first, then say read the first email.')
        return {'id':ids[n]}
    if kind in ('doc','sheet'):
        m=re.search(r'https://docs\.google\.com/'+('document' if kind=='doc' else 'spreadsheets')+r'/d/([A-Za-z0-9_-]{15,150})',text)
        if not m:raise ValueError('Which file? Send its link once. Name discovery needs separate Drive permission.')
        return {'id':m[1],**({'range':'A1:J20'} if kind=='sheet' else {})}
    if kind=='github':
        m=re.search(r'(?:https://github.com/)?([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)',text)
        if not m:raise ValueError('Which public GitHub repository? Give its link or owner/name.')
        return {'repo':m[1]}
    if kind=='calendar':return {}
    raise ValueError('Private read type unavailable.')

def read(uid,body,header=""):
    import time
    session_key=A.digest(header[7:]) if header else ""
    import re
    if set(body)!={'kind','fields'} or not isinstance(body['fields'],dict):raise ValueError('Invalid private read.')
    kind,f=body['kind'],body['fields']
    if set(f)=={'request'}:f=natural_read_fields(uid,kind,f['request'],session_key)
    if kind=='inbox' and set(f)=={'query'} and isinstance(f['query'],str):text=G.inbox(uid,f['query']);db.kv_set('web_mail_results_'+str(uid)+'_'+session_key,{'ids':re.findall(r'ID: ([A-Za-z0-9_-]+)',text),'until':time.time()+600})
    elif kind=='email_read' and set(f)=={'id'} and isinstance(f['id'],str):text=G.read_message(uid,f['id'])
    elif kind=='calendar' and not f:
        text=G.calendar(uid)
    elif kind=='github' and set(f)=={'repo'}:text=json.dumps(__import__('cr_github').digest(uid,f['repo']),ensure_ascii=False)
    elif kind=='doc' and set(f)=={'id'}:text=json.dumps(__import__('cr_workspace').doc_read(uid,f['id']),ensure_ascii=False)
    elif kind=='sheet' and set(f)=={'id','range'}:text=json.dumps(__import__('cr_workspace').sheet_read(uid,f['id'],f['range']),ensure_ascii=False)
    else:raise ValueError('Read not enabled or invalid fields.')
    return {'text':text,'private':True,'stored_in_history':False,'sent_to_ai':False}

def connect(uid,body):
    if set(body)!={'provider'}:raise ValueError('Invalid connection request.')
    if body['provider']=='google':url=G.begin(uid)
    elif body['provider']=='calendar_write':
        url=G.begin(uid,calendar_write=True)
    elif body['provider']=='github':url=__import__('cr_connections').begin(uid,'github')
    elif body['provider']=='workspace':url=__import__('cr_connections').begin(uid,'workspace')
    else:raise ValueError('Connection not enabled.')
    return {'url':url,'text':'Review provider account and permissions. Return here and check Connections. Do not forward your account-bound link.'}
