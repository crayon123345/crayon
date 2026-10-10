"""Explicit requester-bound group actions. No ambient personal-history disclosure."""
import hashlib
import re
import time
import cr_db as db


def action_request(text):
    if text.strip().lower().rstrip('.!') in ('enable my group actions','send it','send','yes send it','send this email','send the email','cancel','cancel draft','cancel email','yes','no'):return True
    if re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',text.strip()):return True
    if re.fullmatch(r'(?i)(?:read|open|show)(?: (?:email|message))? (?:number )?(first|second|third|fourth|fifth|[1-5])',text.strip()):return True
    return bool(re.search(r'(?i)\b(email|e-mail|gmail|inbox|mail|calendar|remind|reminders?|tasks?|memory|remember|forget|delete|privacy|edit|rewrite|revise|change|replace|computer|browse|research|files?|chart|csv|digest|notes?|save|work|project)\b', text) or text.startswith('/'))


def _key(uid, chat, data):
    return 'group_review_' + hashlib.sha256(f'{uid}:{chat}:{data}'.encode()).hexdigest()


class GroupOut:
    """Bind each shown control to the requester and exact group for ten minutes."""
    def __init__(self, out, uid, chat): self.out,self.uid,self.chat=out,uid,chat
    def send(self, chat, text, markup=None):
        if markup and isinstance(markup,dict):
            for row in markup.get('inline_keyboard',[]):
                for b in row:
                    data=b.get('callback_data')
                    if data:db.kv_set(_key(self.uid,self.chat,data),{'until':time.time()+600,'uid':self.uid})
        return self.out.send(chat,text,markup)
    def __getattr__(self,name): return getattr(self.out,name)


def reviewed(uid,chat,data):
    state=db.kv_get(_key(uid,chat,data),None)
    return isinstance(state,dict) and state.get('until',0)>time.time()


def consent(uid,chat,text,out):
    """New audience/data use is disclosed before access. No original request retained."""
    key=f'group_audience_v1_{uid}_{chat}'
    if db.kv_get(key,False):return True
    if text.strip().lower()=='enable my group actions':
        db.kv_set(key,True)
        out.send(chat,'Enabled for your account in this group. Repeat your request; only your own data is used, and results/drafts here are visible to every group member. Send/Create still need your review.')
        return False
    out.send(chat,'Your requested account or memory results and drafts would be visible to everyone in this group. No one else can approve your sends. To allow this for your account here, tag me with "enable my group actions", then repeat the request. Or DM me to keep it private. Google connection links and background alerts stay private.')
    return False
