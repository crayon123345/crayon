"""Opt-in hourly sender/subject/provider-snippet mail check. No model/body reads/actions."""
import time,re,json,html
import cr_db as db
import cr_google as G
import cr_proactive as P
OWNER=1898030949
KEY=lambda uid:'mail_watch_'+str(uid)
def status(uid):
    if type(uid) is not int or uid<=0:raise G.GoogleError('A valid authenticated account is required.')
    state=db.kv_get(KEY(uid),None)
    if not state:return 'Email checks: off. No scheduled inbox checks.'
    from datetime import datetime,timezone
    checked=state.get('checked',0)
    when=datetime.fromtimestamp(checked,timezone.utc).isoformat() if checked else 'not checked'
    return ('Email checks: '+('paused' if state.get('paused') else 'on')+'\nAccount: '+str(state.get('email','unknown'))+'\nLast check/configuration (UTC): '+when+'\nHourly during awake hours, metadata and bounded snippets only. Host timing is best-effort. No email sent or calendar changed. A timestamp alone does not prove a notification was delivered.')
def configure(uid,chat,on):
    if type(uid) is not int or uid<=0:raise G.GoogleError('A valid authenticated account is required.')
    if on and (uid!=chat or uid>=10**15):raise G.GoogleError('Enable email checks from your own private Telegram chat. Browser-only background delivery is not enabled.')
    if not on:db.kv_set(KEY(uid),None);return 'Email checks off.'
    row=G.status(uid)
    if not row:raise G.GoogleError('Connect Google first.')
    now=int(time.time())
    db.kv_set(KEY(uid),{'chat':chat,'email':row['email'],'since':now,'checked':now,'seen':[]})
    return 'Email checks on: hourly during your awake hours, from now onward. No replies or calendar changes. Sender/subject and bounded provider snippet excerpts only, never sent to Gemini. Attention uses Google IMPORTANT/starred labels or subject keywords, so it can miss things. Optional mail gets one short FYI. Free-host timing is best-effort.'
def scan(uid,state):
    if G.status(uid).get('email')!=state['email']:raise G.GoogleError('Connected account changed. Turn email checks on again.')
    messages=G.request(uid,'https://gmail.googleapis.com/gmail/v1/users/me/messages',{'q':'in:inbox after:'+str(state['since']),'maxResults':20})
    if len(state.get('seen',[]))>=500:raise G.GoogleError('Mail watch cache full. Re-enable to start a new watch.')
    ids=[m['id'] for m in messages.get('messages',[]) if m['id'] not in state.get('seen',[])][:4]
    attention=[];optional=[]
    for ident in ids:
        item=G.request(uid,'https://gmail.googleapis.com/gmail/v1/users/me/messages/'+ident,{'format':'metadata','metadataHeaders':['From','Subject']})
        h={v['name'].lower():v['value'] for v in item.get('payload',{}).get('headers',[])}
        from cr_safety import redact,clean_text
        subject=redact(clean_text(html.unescape(h.get('subject','(no subject)'))).replace('\n',' ')[:100])
        sender=redact(clean_text(html.unescape(h.get('from','unknown sender'))).replace('\n',' ')[:100])
        sender=re.sub(r'\s*<[^>]+>','',sender).strip() or 'Unknown sender'
        snippet=redact(clean_text(html.unescape(item.get('snippet',''))).replace('\n',' ')[:140])
        line=subject+'\nFrom: '+sender+('\n'+snippet if snippet else '')
        labels=item.get('labelIds',[])
        target=attention if any(x in labels for x in ('IMPORTANT','STARRED')) or re.search(r'\b(deadline|action required|due|overdue|urgent|interview|exam)\b',subject,re.I) else optional
        target.append(line)
    state['seen']=(state.get('seen',[])+ids)[-500:]
    if not ids:return '',state
    lines=['INBOX CHECK']
    if attention:lines+=['','POSSIBLE ATTENTION','\n\n'.join(attention)]
    if optional:lines+=['','OTHER RECENT MAIL','\n\n'.join(optional)]
    if messages.get('nextPageToken') or len(messages.get('messages',[]))>len(ids):lines+=['','More mail remains. Use /gmail to explore.']
    lines+=['','Checked up to4 messages. Labels/subjects can misjudge importance. No actions taken.']
    return '\n'.join(lines),state
def tick(out):
    # Account IDs derive from stored opt-in keys, never a model or incoming email.
    rows=db.q("SELECT key FROM kv WHERE key LIKE 'mail_watch_%%' AND value IS NOT NULL ORDER BY key LIMIT 500",fetch='all') or []
    for row in rows:
        suffix=row['key'][len('mail_watch_'):]
        if not suffix.isdigit():continue
        uid=int(suffix)
        if not 0<uid<10**15:continue
        tick_user(out,uid)

def tick_user(out,uid):
    state=db.kv_get(KEY(uid),None)
    if not state or state.get('chat')!=uid or state.get('paused') or time.time()-state.get('checked',0)<3600:return
    s=P.settings(uid);n=P.T.now_local(uid)
    if not P.awake(n.hour,int(s['quiet_start']),int(s['quiet_end'])):return
    with P.mem.user_lock(uid):
        state=db.kv_get(KEY(uid),None)
        if not state or state.get('chat')!=uid:return
        try:
            text,state=scan(uid,state)
            if text:out.send(uid,text)
            state['checked']=int(time.time());db.kv_set(KEY(uid),state)
        except G.GoogleError:
            state['checked']=int(time.time());db.kv_set(KEY(uid),state)
            if not state.get('warned'):
                out.send(uid,'Email checks paused by Google access/quota/account error. No actions taken. Reconnect or turn checks off/on after checking the account.')
                state['warned']=True;state['paused']=True;db.kv_set(KEY(uid),state)
