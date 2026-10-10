"""Authenticated private web transport. Ordinary chats mirror to the same Telegram DM.
Foundation stage deliberately blocks external effects and room actions.
"""
import base64
import json
import secrets
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import cr_config as C
import cr_db as db
import cr_web_auth as auth
from cr_safety import clean_text, looks_like_secret

POOL = ThreadPoolExecutor(max_workers=3)
SLOTS = threading.BoundedSemaphore(8)
SCHEMA = '''CREATE TABLE IF NOT EXISTS web_requests(
 id TEXT NOT NULL,user_id BIGINT NOT NULL,state TEXT NOT NULL DEFAULT 'queued',
 encrypted TEXT NOT NULL,created_at TIMESTAMPTZ DEFAULT now(),updated_at TIMESTAMPTZ DEFAULT now(),
 PRIMARY KEY(user_id,id));'''


def init():
    auth.init()
    __import__('cr_web_email_auth').init()
    __import__('cr_web_google_auth').init()
    __import__('cr_accounts').init()
    __import__('cr_web_actions').init()
    __import__('cr_history').init()
    db.q(SCHEMA, fetch='none')
    db.q("ALTER TABLE web_requests ADD COLUMN IF NOT EXISTS progress JSONB DEFAULT '[]'::jsonb",fetch='none')
    __import__('cr_conversations').init()
    db.q('ALTER TABLE web_requests ADD COLUMN IF NOT EXISTS draft TEXT',fetch='none')
    __import__('cr_web_notifications').init()
    __import__('cr_web_rooms').init()


def encode(value):
    return auth._cipher().encrypt(json.dumps(value, default=str).encode()).decode()


def decode(value):
    return json.loads(auth._cipher().decrypt(value.encode()))


class WebOut:
    def __init__(self):
        self.items = []
        self.bytes = 0
    def send(self, chat_id, text, markup=None):
        if len(self.items) >= 30: raise ValueError('Too many response items.')
        self.items.append({'kind':'text','text':clean_text(text)[:16000]})
    def artifact(self, chat_id, item):
        self.bytes += len(item['data'])
        if self.bytes > 4000000 or len(self.items) >= 30 or len(item['data']) > 2000000:
            raise ValueError('Artifact exceeds 2 MB.')
        import os
        self.items.append({'kind':'artifact','name':os.path.basename(item['filename'])[:100],
            'mime':item['mime'],'data':base64.b64encode(item['data']).decode()})
        return len(self.items)
    def typing(self, *args): pass
    def delete(self, *args): return True
    def react(self, *args): return True


def dispatch(uid, name, text):
    import cr_agent as A, cr_memory as M, cr_tools as T, cr_channel
    marker = cr_channel.channel.set('web')
    try:
        with M.user_lock(uid):
            if not db.q('SELECT user_id FROM web_sessions WHERE user_id=%s AND expires_at>now() LIMIT 1',(uid,),'one'):
                raise ValueError('Account was deleted. Sign in again.')
            out = WebOut()
            import cr_accounts
            destination=cr_accounts.telegram_destination(uid)
            chat_destination=destination or uid
            if looks_like_secret(text):
                return [{'kind':'text','text':'That looks like a secret. It was not sent to the model or saved. Do not paste credentials here.'}]
            M.touch_user(uid, name)
            if __import__('cr_natural').handle(uid,uid,text,out):return out.items
            text=__import__('cr_natural').translate(text)
            if __import__('cr_followups').handle(uid,chat_destination,text,out):return out.items
            if __import__('cr_plugins').handle(uid,uid,text,out):return out.items
            if __import__('cr_persona').handle(uid,uid,text,out):return out.items
            if __import__('cr_voice').handle(uid,uid,text,out):return out.items
            if __import__('cr_games').handle(uid,uid,text,out):return out.items
            simple = text.strip().lower()
            if simple in ('my tasks','task dashboard') or text.startswith('/tasks'):
                import cr_dashboard
                cr_dashboard.handle(uid, uid, text, out)
                return out.items
            if text.startswith('/work') or simple in ('my work queue','show my work queue') or text.lower().startswith(('background research:','work in background:')):
                import cr_work
                cr_work.handle(uid,chat_destination,text,out)
                return out.items
            if text.startswith(('/browse ','/computer ')):
                cmd,arg=text.split(None,1)
                if cmd=='/computer' and arg.strip()=='status':out.send(uid,json.dumps(__import__('cr_computer').status(uid)))
                else:
                    intent=('Browser screenshot: ' if cmd=='/browse' else 'On my computer ')+arg
                    reply,meta=A.respond(uid,chat_destination,intent,name,channel_name='web')
                    out.send(uid,reply)
                    for item in meta.get('artifacts',[]):out.artifact(uid,item)
                return out.items
            if simple in ('my reminders','/reminders'):
                rows = T.list_reminders({'uid':uid})['reminders']
                return [{'kind':'text','text':json.dumps(rows, default=str, ensure_ascii=False)}]
            if simple in ('show my memory','/memory'):
                return [{'kind':'text','text':M.render_memory(uid)}]
            if simple in ('help','/help'):
                return [{'kind':'text','text':'Web: chat, memory, notes, tasks, research, calculations, CSV/charts, computer/browser and /work controls. Standalone reminder/work results appear in Menu > Notifications, not phone/email/push. Telegram-backed reminders still arrive in Telegram. Uploads use +. Google actions use Menu > Connections. Group rooms/deletion remain locked.'}]
            # Do not fall into the model for features whose channel review is not implemented yet.
            import re
            if re.search(r'(?i)\b(?:send|write|draft|compose|prepare)\s+(?:an?\s+)?(?:e-?mail|mail)\b',text):
                return [{'kind':'text','text':'I can prepare an email for exact review, not auto-send it. Use the web composer to draft it; connect Google in Menu > Connections if needed. No email sent.'}]
            if re.search(r'(?i)\b(gmail|inbox|email|e-mail|mail|calendar|google|workspace|github|sheet|doc|booking|book|delete|wipe|forget|digest|proactive|watch)\b', text) or text.startswith(('/email','/google','/connect','/calendar','/delete','/forget')):
                return [{'kind':'text','text':'For Google reads, email/calendar/Sheet previews use Menu > Connections / actions. Rooms and deletion are not enabled in web chat. No external action was made.'}]
            if simple in ('yes','confirm','go ahead','do it','send it'):
                return [{'kind':'text','text':'Web confirmations are not enabled yet. Nothing was sent or deleted.'}]
            # Never consume a pending action created in another channel.
            goal=text.startswith('/goal') and (text=='/goal' or text[5:6].isspace())
            prompt=text[5:].strip() if goal else text
            if goal and not prompt:
                return [{'kind':'text','text':'Use /goal followed by a concrete research or calculation goal.'}]
            reply, meta = A.respond(uid, chat_destination, prompt, name, channel_name='web', goal_mode=goal)
            out.send(uid, reply)
            out.items.extend({'kind':'mcp_review',**r} for r in meta.get('mcp_reviews',[]))
            for item in meta.get('artifacts', []):
                out.artifact(uid, item)
            return out.items
    finally:
        cr_channel.channel.reset(marker)


def submit(user, body):
    text, ident = body.get('message'), body.get('request_id')
    if set(body)-{'reply_to'} not in ({'message','request_id'},{'message','request_id','conversation_id'}) or not isinstance(text,str) or not 1 <= len(text.strip()) <= 8000 or not isinstance(ident,str) or not auth.PATTERN.fullmatch(ident):
        raise ValueError('Invalid message request.')
    uid = user['user_id']
    conversation=body.get('conversation_id')
    if conversation:__import__('cr_conversations').require(uid,conversation)
    if looks_like_secret(text):
        raise ValueError('Do not send passwords, keys or credentials here.')
    quote=__import__('cr_reply_context').web(uid,conversation,body['reply_to']) if body.get('reply_to') else None
    old = db.q('SELECT state FROM web_requests WHERE user_id=%s AND id=%s', (uid,ident), 'one')
    if old:
        return {'request_id':ident,'state':old['state']}
    count = db.q("SELECT (SELECT count(*) FROM web_requests WHERE user_id=%s AND created_at>now()-interval '24 hours')+(SELECT count(*) FROM messages WHERE user_id=%s AND role='user' AND ts>now()-interval '24 hours') AS n", (uid,uid), 'one')['n']
    if count >= C.DAILY_MESSAGE_CAP:
        raise ValueError('Daily free-tier message limit reached.')
    if not SLOTS.acquire(blocking=False):
        raise ValueError('Crayon is busy. Wait before sending another request.')
    try:
        row = db.q('INSERT INTO web_requests(id,user_id,encrypted) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id',
            (ident,uid,encode({'input':text,'name':user['name'],'conversation_id':conversation,'reply_context':quote})), 'one')
        if row:
            POOL.submit(_run,uid,ident)
        else:
            SLOTS.release()
    except Exception:
        SLOTS.release()
        raise
    return {'request_id':ident,'state':'queued'}


def _run(uid, ident):
    try:
        row = db.q("UPDATE web_requests SET state='running',updated_at=now() WHERE user_id=%s AND id=%s AND state='queued' RETURNING encrypted",(uid,ident),'one')
        if not row: return
        data = decode(row['encrypted'])
        import cr_conversations as conversations
        conversation=data.get('conversation_id')
        if conversation:conversations.require(uid,conversation)
        conversation_token=conversations.current.set(conversation)
        import cr_reply_context as R
        reply_token=R.current.set(data.get('reply_context'))
        import cr_progress
        def report(event):
            db.q("UPDATE web_requests SET progress=(CASE WHEN jsonb_array_length(progress)<16 THEN progress ELSE progress - 0 END)||%s::jsonb WHERE user_id=%s AND id=%s AND state='running'",(json.dumps([event]),uid,ident),'none')
        import cr_stream
        def stream_report(text):
            db.q('UPDATE web_requests SET draft=%s,updated_at=now() WHERE user_id=%s AND id=%s AND state=%s',(encode({'text':text}),uid,ident,'running'),'none')
        stream_token=cr_stream.callback.set(stream_report)
        events_token=cr_progress.recorded.set([])
        progress_token=cr_progress.callback.set(report)
        cr_progress.emit('Request accepted','done')
        try:
            items = dispatch(uid,data['name'],data['input'])
            try:
                import re
                if looks_like_secret(data['input']) or re.search(r'(?i)\b(gmail|inbox|email|e-mail|mail|calendar|google|workspace|github|sheet|doc|booking|book|delete|wipe|forget|digest|proactive|watch)\b',data['input']) or data['input'].startswith(('/email','/google','/connect','/calendar','/delete','/forget')):
                    raise StopIteration
                import cr_accounts
                destination=cr_accounts.telegram_destination(uid)
                if destination is None:raise StopIteration
                import cr_telegram
                channel_out=cr_telegram.Out()
                channel_out.send(destination,'[From web] '+data['input'])
                for item in items:
                    if item['kind']=='text':channel_out.send(destination,item['text'])
                    elif item['kind']=='artifact':channel_out.artifact(destination,{'filename':item['name'],'mime':item['mime'],'data':base64.b64decode(item['data'])})
            except StopIteration:pass
            except Exception:
                items.append({'kind':'text','text':'Web reply completed, but Telegram sync was not confirmed. No automatic resend. Check your Telegram chat.'})
            status = 'done'
        except Exception:
            items = [{'kind':'text','text':'Request stopped. Its outcome is unconfirmed. Check your records before trying again.'}]
            status = 'blocked'
        db.q('UPDATE web_requests SET state=%s,encrypted=%s,draft=NULL,updated_at=now() WHERE user_id=%s AND id=%s',
             (status,encode({'items':items}),uid,ident),'none')
        if status=='done' and conversation:
            try:conversations.auto_title(uid,conversation,data['input'])
            except Exception:pass
    finally:
        if 'stream_token' in locals():cr_stream.callback.reset(stream_token)
        if 'progress_token' in locals():cr_progress.callback.reset(progress_token)
        if 'events_token' in locals():cr_progress.recorded.reset(events_token)
        if 'reply_token' in locals():R.current.reset(reply_token)
        if 'conversation_token' in locals():conversations.current.reset(conversation_token)
        SLOTS.release()


def result(uid, ident):
    if not isinstance(ident,str) or not auth.PATTERN.fullmatch(ident): raise ValueError('Invalid request ID.')
    row = db.q('SELECT state,encrypted,updated_at,progress,draft FROM web_requests WHERE user_id=%s AND id=%s', (uid,ident),'one')
    if not row: raise ValueError('Request not found in your account.')
    state = row['state']
    if state in ('queued','running') and (datetime.now(row['updated_at'].tzinfo)-row['updated_at']).total_seconds()>300:
        # No automatic rerun of interrupted effects.
        state = 'blocked'
    return {'request_id':ident,'state':state,'draft':decode(row['draft']).get('text','') if row.get('draft') and state=='running' else '', 'progress':row.get('progress') or [],'items':decode(row['encrypted']).get('items',[]) if state in ('done','blocked') else []}


def activity(uid):
    # Requests may include private results, so output is encrypted at rest and owner-scoped.
    rows = db.q('SELECT id,state,encrypted,created_at FROM web_requests WHERE user_id=%s ORDER BY created_at DESC LIMIT 30', (uid,))
    return [{'id':r['id'],'state':r['state'],'created_at':str(r['created_at']),
        'items':decode(r['encrypted']).get('items',[]) if r['state'] in ('done','blocked') else []} for r in reversed(rows)]


def history(uid, before=None):
    from datetime import datetime
    if before is not None:
        try:cursor=datetime.fromisoformat(before)
        except Exception:raise ValueError('Invalid history cursor.') from None
        if cursor.tzinfo is None:raise ValueError('History cursor needs timezone.')
    params=(uid,cursor,uid,cursor) if before else (uid,uid)
    condition=' AND ts<%s' if before else ''
    rows=db.q("SELECT 'message' AS kind,id::text AS id,role,content,NULL::text AS encrypted,ts,work_events,encrypted_media FROM messages WHERE user_id=%s"+condition+
      " UNION ALL SELECT 'receipt',id,'receipt',NULL,encrypted,ts,'[]'::jsonb,NULL::text FROM channel_history WHERE user_id=%s"+condition+" ORDER BY ts DESC,id DESC LIMIT 51",params)
    page=rows[:50];items=[]
    for r in reversed(page):
        if r['kind']=='receipt':
            data=json.loads(auth._cipher().decrypt(r['encrypted'].encode()))
            for role in ('user','assistant'):items.append({'id':r['id']+role,'role':role,'text':data[role],'time':str(r['ts']),'media_missing':False})
        else:items.append({'id':r['id'],'role':r['role'],'text':r['content'],'time':str(r['ts']),'work_events':r.get('work_events') or [],'media':decode(r['encrypted_media']).get('items',[]) if r.get('encrypted_media') else [],'media_missing':r['content'].startswith('[User sent media:')})
    return {'messages':items,'has_more':len(rows)>50,'before':page[-1]['ts'].isoformat() if page else None}


def upload(user, body):
    import os, cr_media
    if set(body) not in ({'data','mime','name','caption','request_id'},{'data','mime','name','caption','request_id','conversation_id'}):
        raise ValueError('Invalid upload fields.')
    if not isinstance(body['name'],str) or not 1<=len(body['name'])<=200 or not isinstance(body['mime'],str) or len(body['mime'])>100:
        raise ValueError('Invalid file metadata.')
    if not isinstance(body['caption'],str) or len(body['caption'])>8000 or looks_like_secret(body['caption']):
        raise ValueError('Invalid caption or secret detected.')
    if not isinstance(body['data'],str) or len(body['data'])>26666672:
        raise ValueError('Upload limit is 20 MB.')
    try: data=base64.b64decode(body['data'],validate=True)
    except Exception: raise ValueError('Invalid upload encoding.') from None
    if not data or len(data)>cr_media.MAX_BYTES:raise ValueError('Upload limit is 20 MB.')
    ident=body['request_id'];uid=user['user_id']
    conversation=body.get('conversation_id')
    if conversation:__import__('cr_conversations').require(uid,conversation)
    if not isinstance(ident,str) or not auth.PATTERN.fullmatch(ident):raise ValueError('Invalid request ID.')
    old=db.q('SELECT state FROM web_requests WHERE user_id=%s AND id=%s',(uid,ident),'one')
    if old:return {'request_id':ident,'state':old['state']}
    count=db.q("SELECT (SELECT count(*) FROM web_requests WHERE user_id=%s AND created_at>now()-interval '24 hours')+(SELECT count(*) FROM messages WHERE user_id=%s AND role='user' AND ts>now()-interval '24 hours') AS n",(uid,uid),'one')['n']
    if count>=C.DAILY_MESSAGE_CAP:raise ValueError('Daily free-tier message limit reached.')
    if not SLOTS.acquire(blocking=False):raise ValueError('Crayon is busy. Wait before uploading.')
    name=os.path.basename(body['name']);mime=cr_media.normalize_mime(body['mime'],name)
    try:
        row=db.q('INSERT INTO web_requests(id,user_id,encrypted) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING RETURNING id',
            (ident,uid,encode({'name':name,'mime':mime,'bytes':len(data),'caption':body['caption'],'raw_retained':False})),'one')
        if row:POOL.submit(_upload_run,uid,ident,user['name'],data,mime,name,body['caption'],conversation)
        else:SLOTS.release()
    except Exception:SLOTS.release();raise
    return {'request_id':ident,'state':'queued'}


def _upload_run(uid,ident,username,data,mime,name,caption,conversation=None):
    try:
        row=db.q("UPDATE web_requests SET state='running',updated_at=now() WHERE user_id=%s AND id=%s AND state='queued' RETURNING id",(uid,ident),'one')
        if not row:return
        import cr_media,cr_memory as M,cr_conversations as conversations
        if conversation:conversations.require(uid,conversation)
        conversation_token=conversations.current.set(conversation)
        try:
            with M.user_lock(uid):
                if not db.q('SELECT user_id FROM web_sessions WHERE user_id=%s AND expires_at>now() LIMIT 1',(uid,),'one'):raise ValueError('Account was deleted.')
                M.touch_user(uid,username)
                response=cr_media.analyze(data,mime,caption,name)
                M.add_message(uid,'user','[User sent media: '+mime+'] '+name+' '+caption)
                M.add_message(uid,'assistant','[Media analysis summary; raw file not retained] '+clean_text(response)[:3000])
            items=[{'kind':'text','text':clean_text(response)}];state='done'
            try:
                import cr_accounts
                destination=cr_accounts.telegram_destination(uid)
                if destination is None:raise StopIteration
                import cr_telegram
                channel_out=cr_telegram.Out()
                channel_out.send(destination,'[Web upload] '+name+' ('+mime+'). Original file not retained here.')
                channel_out.send(destination,clean_text(response))
            except StopIteration:pass
            except Exception:items.append({'kind':'text','text':'Upload analysis completed, but Telegram sync was not confirmed. No automatic resend.'})
        except Exception as e:
            items=[{'kind':'text','text':'Upload stopped: '+(str(e)[:200] if isinstance(e,ValueError) else 'Media processing failed. No analysis confirmed.')}];state='blocked'
        db.q('UPDATE web_requests SET state=%s,encrypted=%s,draft=NULL,updated_at=now() WHERE user_id=%s AND id=%s',
             (state,encode({'items':items,'file_receipt':{'name':name,'mime':mime,'bytes':len(data),'raw_retained':False}}),uid,ident),'none')
    finally:
        if 'conversation_token' in locals():conversations.current.reset(conversation_token)
        SLOTS.release()
