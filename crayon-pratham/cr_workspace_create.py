"""User-bound reviewed native creation. No shares, retries or inferred content."""
import json,re,hashlib,secrets
import cr_db as db
import cr_google as G
import cr_connections as X
import cr_workspace as W

KINDS={'doc','sheet','slides'}
def fields(kind,title,content):
    from cr_safety import looks_like_secret
    if kind not in KINDS or not isinstance(title,str) or not 1<=len(title.strip())<=100:raise ValueError('Use doc, sheet or slides and a title of1-100characters.')
    if looks_like_secret(json.dumps(content)):raise ValueError('Do not include credentials.')
    if kind=='doc':
        if not isinstance(content,str) or not 1<=len(content)<=12000:raise ValueError('Document content:1-12000characters.')
    elif kind=='sheet':
        if not isinstance(content,list) or not content or len(content)>20 or any(not isinstance(r,list) or not r or len(r)>10 for r in content):raise ValueError('Sheet content:1-20rows,1-10cells per row.')
        if any(type(v) not in (str,int,float,bool) or len(str(v))>500 for r in content for v in r):raise ValueError('Use bounded RAW cell values.')
        json.dumps(content,allow_nan=False)
    else:
        if not isinstance(content,list) or not 1<=len(content)<=10 or any(not isinstance(s,dict) or set(s)!={'title','body'} or not isinstance(s['title'],str) or not isinstance(s['body'],str) or not 1<=len(s['title'])<=100 or len(s['body'])>1000 for s in content):raise ValueError('Slides content:1-10objects with title(up to100) and body(up to1000).')
    return {'kind':kind,'title':title.strip(),'content':content}

def preview(uid,kind,title,content):
    payload=fields(kind,title,content)
    account=(X.status(uid,'workspace') or {}).get('identity')
    if not account:raise ValueError('Connect your own Workspace account first. Gmail/calendar connection is separate.')
    if kind=='slides':
        row=db.q('SELECT encrypted_tokens FROM service_connections WHERE user_id=%s AND provider=%s',(uid,'workspace'),'one')
        scopes=G.decrypt(uid,row['encrypted_tokens']).get('tokens',{}).get('scope','').split()
        if 'https://www.googleapis.com/auth/presentations' not in scopes:raise ValueError('Reconnect Workspace and approve Slides permission first.')
    payload['account']=account
    return {'payload':payload,'text':'Create '+kind+' preview, not created.\nAccount: '+account+'\nTitle: '+payload['title']+'\nContent:\n'+(content if isinstance(content,str) else json.dumps(content,ensure_ascii=False))+'\nPrivate new file only. No shares or invites. Creation and population are separate writes; partial failure may leave an incomplete file. No automatic retry. Basic native content, not a visually verified polished deliverable.'}

def apply(uid,p):
    fields(p['kind'],p['title'],p['content'])
    if (X.status(uid,'workspace') or {}).get('identity')!=p['account']:raise ValueError('Workspace account changed. Prepare a new review.')
    kind=p['kind'];ident=None;url=None
    try:
        if kind=='doc':
            r=W._call(uid,'POST','https://docs.googleapis.com/v1/documents',{'title':p['title']});ident=W.file_id(r['documentId']);url='https://docs.google.com/document/d/'+ident+'/edit'
            fresh=W._call(uid,'GET','https://docs.googleapis.com/v1/documents/'+ident)
            W._call(uid,'POST','https://docs.googleapis.com/v1/documents/'+ident+':batchUpdate',{'requests':[{'insertText':{'location':{'index':1},'text':p['content']}}],**({'writeControl':{'requiredRevisionId':fresh['revisionId']}} if fresh.get('revisionId') else {})})
            got=W.doc_read(uid,ident);verified=got['text'].strip()==p['content'].strip()
            url='https://docs.google.com/document/d/'+ident+'/edit'
        elif kind=='sheet':
            r=W._call(uid,'POST','https://sheets.googleapis.com/v4/spreadsheets',{'properties':{'title':p['title']},'sheets':[{'properties':{'title':'Sheet1'}}]});ident=W.file_id(r['spreadsheetId']);url=r.get('spreadsheetUrl')
            rows=p['content'];cols=max(map(len,rows));area='Sheet1!A1:'+chr(64+cols)+str(len(rows))
            from urllib.parse import quote
            W._call(uid,'PUT','https://sheets.googleapis.com/v4/spreadsheets/'+ident+'/values/'+quote(area,safe=''),{'range':area,'majorDimension':'ROWS','values':rows},{'valueInputOption':'RAW'})
            got=W.sheet_read(uid,ident,area,raw=True);verified=got.get('values')==rows
        else:
            # Refresh scope/read capability before the create write.
            preview(uid,kind,p['title'],p['content'])
            r=W._call(uid,'POST','https://slides.googleapis.com/v1/presentations',{'title':p['title']});ident=W.file_id(r['presentationId']);url='https://docs.google.com/presentation/d/'+ident+'/edit';requests=[]
            for n,s in enumerate(p['content']):
                sid='crayon_slide_'+str(n);tid='crayon_title_'+str(n);bid='crayon_body_'+str(n)
                requests.append({'createSlide':{'objectId':sid,'slideLayoutReference':{'predefinedLayout':'TITLE_AND_BODY'},'placeholderIdMappings':[{'layoutPlaceholder':{'type':'TITLE'},'objectId':tid},{'layoutPlaceholder':{'type':'BODY'},'objectId':bid}]}})
                requests.extend([{'insertText':{'objectId':tid,'text':s['title']}},{'insertText':{'objectId':bid,'text':s['body'] or ' '}}])
            W._call(uid,'POST','https://slides.googleapis.com/v1/presentations/'+ident+':batchUpdate',{'requests':requests})
            got=W._call(uid,'GET','https://slides.googleapis.com/v1/presentations/'+ident);verified=len(got.get('slides',[]))==len(p['content']) and all(s['title'] in json.dumps(got,ensure_ascii=False) and s['body'] in json.dumps(got,ensure_ascii=False) for s in p['content'])
            url='https://docs.google.com/presentation/d/'+ident+'/edit'
        return {'created':True,'id':ident,'url':url,'content_readback_verified':verified,'visual_verified':False,'note':'Native content created. Visual layout not verified; inspect before sharing. No sharing changed.'}
    except Exception:
        return {'created':bool(ident),'id':ident,'url':url,'content_readback_verified':False,'visual_verified':False,'note':'Creation stopped or uncertain. An incomplete file may exist. Check Workspace before another request; no retry.'}

def init():
    db.q('CREATE TABLE IF NOT EXISTS workspace_create_reviews(id TEXT PRIMARY KEY,user_id BIGINT,chat_id BIGINT,encrypted TEXT,content_hash TEXT,status TEXT DEFAULT \'pending\',expires_at TIMESTAMPTZ)',fetch='none')

def prepare_chat(uid,chat,kind,title,content):
    if uid!=chat:raise ValueError('Create Workspace files in your private chat only.')
    d=preview(uid,kind,title,content);digest=hashlib.sha256(json.dumps(d['payload'],sort_keys=True).encode()).hexdigest();ident=secrets.token_hex(12)
    init();db.q("INSERT INTO workspace_create_reviews(id,user_id,chat_id,encrypted,content_hash,expires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')",(ident,uid,chat,G.encrypt(uid,d['payload']),digest),'none')
    return {**d,'id':ident,'hash':digest[:12]}

def confirm_chat(uid,chat,ident,h,decision):
    if uid!=chat or decision not in ('confirm','cancel'):raise ValueError('Use your own exact private review.')
    init();r=db.q("UPDATE workspace_create_reviews SET status='claimed' WHERE id=%s AND user_id=%s AND chat_id=%s AND left(content_hash,12)=%s AND status='pending' AND expires_at>now() RETURNING encrypted,content_hash",(ident,uid,chat,h),'one')
    if not r:raise ValueError('Review expired, changed or used. No retry.')
    if decision=='cancel':return 'Cancelled. No file created.'
    p=G.decrypt(uid,r['encrypted'])
    if hashlib.sha256(json.dumps(p,sort_keys=True).encode()).hexdigest()!=r['content_hash']:raise ValueError('Review changed.')
    return json.dumps(apply(uid,p),ensure_ascii=False)

def natural_fields(text):
    from cr_safety import looks_like_secret
    if not isinstance(text,str) or not 1<=len(text)<=8000 or looks_like_secret(text):raise ValueError('Give a bounded request without secrets.')
    match=re.search(r'(?i)\b(?:make|create|prepare|build|draft)\b.*?\b(doc(?:ument)?|sheet|spreadsheet|slides|presentation)\b',text)
    if not match:return None
    kind={'document':'doc','spreadsheet':'sheet','presentation':'slides'}.get(match[1].lower(),match[1].lower())
    import cr_llm as L
    result=L.ask_json(text,system='Prepare a new private Google '+kind+' from only this user request. Return JSON {"title":"...","content":...}. Doc content is plain text up to12000characters; Sheet content is an array up to20rows/10cells with raw values; Slides content is1-10objects with title/body. Do the writing and structure. Never invent personal expenses, notes, dates or facts. If personal source material is missing return {"question":"Ask for that material"}. General educational content may be composed. No private sources, tools, sharing, sends, markup or formulas. Not a create action; user reviews the full result first.',default={}) or {}
    if result.get('question'):raise ValueError(str(result['question'])[:250])
    return fields(kind,result.get('title'),result.get('content'))

def handle(uid,chat,text,out):
    match=re.match(r'(?is)^create (?:google )?(doc|sheet|slides)\s+(.+)$',text.strip())
    candidate=re.search(r'(?i)\b(?:make|create|prepare|build|draft)\b.*?\b(doc(?:ument)?|sheet|spreadsheet|slides|presentation)\b',text)
    if not candidate:return False
    try:
        if match and ' | ' in match[2]:
            kind=match[1].lower();parts=match[2].split(' | ',1)
            f=fields(kind,parts[0],parts[1] if kind=='doc' else json.loads(parts[1]))
        else:f=natural_fields(text)
        d=prepare_chat(uid,chat,f['kind'],f['title'],f['content'])
        out.send(chat,d['text'],markup={'inline_keyboard':[[{'text':'Create exactly this','callback_data':'workspace_create:confirm:'+d['id']+':'+d['hash']},{'text':'Cancel','callback_data':'workspace_create:cancel:'+d['id']+':'+d['hash']}]]})
    except Exception as e:out.send(chat,'Workspace create preview not ready: '+str(e)[:300])
    return True
