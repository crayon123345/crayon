"""Telegram exact one-use sheet preview, separate from web session tickets."""
import json,hashlib,secrets
import cr_db as db
import cr_google as G
import cr_workspace as S

def init():
    db.q('''CREATE TABLE IF NOT EXISTS workspace_reviews(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,
      chat_id BIGINT NOT NULL,encrypted TEXT NOT NULL,content_hash TEXT NOT NULL,
      expires_at TIMESTAMPTZ NOT NULL,status TEXT DEFAULT 'pending')''',fetch='none')

def preview(uid,chat,sid,area,values):
    if uid!=chat:raise ValueError('Prepare Sheet changes in your private chat.')
    d=S.sheet_preview(uid,sid,area,values)
    import cr_connections as X
    account=(X.status(uid,'workspace') or {}).get('identity')
    if not account:raise ValueError('Connect your own Workspace account first.')
    content={'account':account,'preview':d}
    h=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest();ident=secrets.token_hex(12)
    init();db.q('DELETE FROM workspace_reviews WHERE user_id=%s OR expires_at<now()',(uid,),'none')
    db.q("INSERT INTO workspace_reviews(id,user_id,chat_id,encrypted,content_hash,expires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')",(ident,uid,chat,G.encrypt(uid,content),h),'none')
    text='Sheet update preview, not written.\nAccount: '+account+'\nSpreadsheet: '+sid+'\nRange: '+area+'\nBefore: '+json.dumps(d['payload']['before'])+'\nWrite RAW values: '+json.dumps(values)+'\nNo formulas. Ten-minute one-use review, compare before write, then readback. No automatic retry.'
    return {'id':ident,'hash':h[:12],'text':text}

def confirm(uid,chat,ident,h,decision):
    if uid!=chat or decision not in ('confirm','cancel'):raise ValueError('Use your own private review.')
    init()
    row=db.q("UPDATE workspace_reviews SET status='claimed' WHERE id=%s AND user_id=%s AND chat_id=%s AND left(content_hash,12)=%s AND status='pending' AND expires_at>now() RETURNING encrypted,content_hash",(ident,uid,chat,h),'one')
    if not row:raise ValueError('Review expired, changed, already used or belongs to another account.')
    if decision=='cancel':return 'Cancelled. No Sheet change made.'
    content=G.decrypt(uid,row['encrypted'])
    import cr_connections as X
    if hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()!=row['content_hash'] or (X.status(uid,'workspace') or {}).get('identity')!=content['account']:raise ValueError('Account/review changed. No write made.')
    try:return json.dumps(S.sheet_apply(uid,content['preview']['payload'],content['preview']['hash']),ensure_ascii=False)
    except Exception:raise ValueError('Sheet write stopped or uncertain. Check the Sheet; do not retry automatically.') from None

def handle(uid,chat,text,out):
    if not __import__('re').search(r'(?i)\b(?:update|change|edit|write|fill)\b.*\b(?:sheet|spreadsheet)\b',text):return False
    try:
        args=text[len('update sheet '):].split(' | ')
        if len(args)==3:d=preview(uid,chat,args[0],args[1],json.loads(args[2]))
        else:
            f=__import__('cr_natural').sheet_fields(text);d=preview(uid,chat,f['sid'],f['area'],f['values'])
        out.send(chat,d['text'],markup={'inline_keyboard':[[{'text':'Update exactly this','callback_data':'sheet:confirm:'+d['id']+':'+d['hash']},{'text':'Cancel','callback_data':'sheet:cancel:'+d['id']+':'+d['hash']}]]})
    except Exception as e:out.send(chat,'Sheet preview not ready: '+str(e)[:250])
    return True
