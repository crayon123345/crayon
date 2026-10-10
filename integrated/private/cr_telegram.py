"""Telegram transport (httpx). Long-polling or webhook, both feed handle_update()."""
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import httpx

import cr_agent as A
import cr_config as C
import cr_db as db
import cr_memory as mem
from cr_safety import redact, looks_like_secret, clean_text

log = logging.getLogger("crayon.tg")
_http = httpx.Client(timeout=httpx.Timeout(45.0, connect=10.0))
_pool = ThreadPoolExecutor(max_workers=6)

HELP = """I'm Crayon. Just tell me what you need.

Try saying:
"Remind me tomorrow at 8am to call Mum."
"Help me plan a project and track the steps."
"Remember that I prefer short answers."
"What do you remember about me?"
"Any new mail from Alex?"
"What's on my calendar?"
"Email alex@example.com saying the meeting moved to 5."

I'll show the email first. Tap Send or say "send it" after reviewing it. You can cancel instead. I never send an unreviewed email.

You can send photos, documents, audio or video up to 20 MB. Ask follow-up questions about them.

Say "connect Google", "my reminders", "my tasks", "memory review" or "help". Menu: /browse, /computer, /research, /files, /email_checks, /calendar_slot. Calendar slots are private previews; Create is always reviewed. Hourly email checks use your own Google connection and respect quiet hours. Browser/computer are available with per-user limits and private file folders. For updates, say "turn on daily check-ins" or "morning digest". Google is in testing mode, so only approved testers can connect and access may need renewing after 7 days.

In a group, tag @crayon_v1_bot. After your own group-audience opt-in, requested account/memory results and reviewed actions can appear there for everyone to see. Only you can confirm your controls. Connection links and background alerts stay private.

Say "privacy options" for data controls. Advanced slash commands still work."""



def api(method, **params):
    r = _http.post(f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/{method}", json=params)
    try:
        data = r.json()
    except Exception:
        raise RuntimeError(f"telegram {method}: HTTP {r.status_code}")
    if not data.get("ok"):
        raise RuntimeError(f"telegram {method}: {data.get('description')}")
    return data["result"]


class Out:
    """Real sender. Tests use CaptureOut with the same interface."""
    def send(self, chat_id, text, markup=None):
        import cr_whatsapp as W
        if W.owns(chat_id):
            return W.WhatsAppOut().send(chat_id, text, markup)
        text = clean_text(text) or "(empty)"
        chunks = [text[i:i + 3900] for i in range(0, len(text), 3900)]
        for i, ch in enumerate(chunks):
            params = {"chat_id": chat_id, "text": ch, "disable_web_page_preview": True}
            if markup and i == len(chunks) - 1:
                params["reply_markup"] = markup
            api("sendMessage", **params)

    def artifact(self,chat_id,item):
        import cr_whatsapp as W
        if W.owns(chat_id):return W.WhatsAppOut().artifact(chat_id,item)
        if len(item['data'])>2000000:raise ValueError('attachment too large')
        method,field='sendDocument','document'
        if item['mime']=='audio/mpeg':method,field='sendVoice','voice'
        if item['mime']=='image/png':
            import io
            from PIL import Image
            with Image.open(io.BytesIO(item['data'])) as image:
                w,h=image.size
            if len(item['data'])<=10000000 and w+h<=10000 and max(w,h)/min(w,h)<=20:method,field='sendPhoto','photo'
        r=_http.post(f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/{method}",data={'chat_id':str(chat_id)},files={field:(item['filename'],item['data'],item['mime'])})
        row=r.json()
        if not row.get('ok'):raise RuntimeError('Telegram attachment not confirmed')
        return row['result']['message_id']

    def react(self, chat_id, message_id, emoji):
        try:
            api("setMessageReaction", chat_id=chat_id, message_id=message_id,
                reaction=[{"type":"emoji", "emoji":emoji}], is_big=False)
            return True
        except Exception as e:
            log.warning("reaction failed: %s", redact(str(e))[:180])
            return False

    def typing(self, chat_id):
        try:
            api("sendChatAction", chat_id=chat_id, action="typing")
        except Exception:
            pass

    def delete(self, chat_id, message_id):
        try:
            api("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            return False


class CaptureOut:
    def __init__(self):
        self.sent, self.meta, self.reactions = [], {}, []

    def send(self, chat_id, text, markup=None):
        self.sent.append({"text": clean_text(text), "markup": bool(markup)})

    def react(self, chat_id, message_id, emoji):
        self.reactions.append({"emoji":emoji,"message_id":message_id})
        return True

    def artifact(self,chat_id,item):
        self.sent.append({'text':'Attachment: '+item['filename'],'markup':False})
        return 1

    def typing(self, chat_id):
        pass

    def delete(self, chat_id, message_id):
        return True


class ProgressOut:
    """Serialize timed progress with the first reply so progress never follows it."""
    def __init__(self,out,chat_id,lines):
        self.out,self.chat_id=out,chat_id
        self.finished=threading.Event()
        self.lock=threading.Lock()
        self.lines=lines
    def __getattr__(self,name):return getattr(self.out,name)
    def send(self,*args,**kwargs):
        with self.lock:
            self.finished.set()
            return self.out.send(*args,**kwargs)
    def emit(self,line):
        with self.lock:
            if self.finished.is_set():return False
            try:self.out.send(self.chat_id,line)
            except Exception:pass
            return True
    def run(self):
        for delay,line in self.lines:
            if self.finished.wait(delay):return
            if not self.emit(line):return
    def start(self):
        threading.Thread(target=self.run,daemon=True).start()
        return self
    def stop(self):
        with self.lock:self.finished.set()


def _over_cap(uid):
    r = db.q("SELECT count(*) AS n FROM messages WHERE user_id=%s AND role='user' AND ts > now() - interval '24 hours'", (uid,), "one")
    return r["n"] >= C.DAILY_MESSAGE_CAP


def handle_update(upd, out=None):
    out = out or Out()
    try:
        if "callback_query" in upd:
            return handle_callback(upd["callback_query"], out)
        msg = upd.get("message")
        if not msg:
            return
        chat_id = msg["chat"]["id"]
        uid = msg["from"]["id"]
        name = (msg["from"].get("first_name") or "").strip()
        if msg["chat"].get("type") in ("group","supergroup"):
            import cr_group
            log.info("group update chat=%s message=%s tagged=%s service=%s",chat_id,msg.get("message_id"),cr_group.mentioned(msg),bool(msg.get("new_chat_members")))
            if any(x.get("username", "").lower()==cr_group.BOT_USERNAME for x in msg.get("new_chat_members", [])):
                out.send(chat_id,"Hi, I'm Crayon. Tag @crayon_v1_bot to chat. Group actions need your own audience opt-in; results are visible to everyone here.")
                return
            if not cr_group.mentioned(msg):return
            import cr_group_actions as GA,re
            text=re.sub(r'@'+re.escape(cr_group.BOT_USERNAME)+r'\b','',msg.get('text',''),flags=re.I).strip()
            # Direct group request selects this audience, never another member's identity.
            pending_compose=False
            if __import__('cr_games').handle(msg.get('from',{}).get('id'),chat_id,text,out):return
            if cr_group.mentioned(msg):
                # Only an explicitly tagged answer may resume this requester's compose.
                import cr_google as Google,time
                try:
                    blob=db.kv_get('google_compose_'+str(uid),None)
                    state=Google.decrypt(uid,blob) if blob else {}
                    pending_compose=state.get('chat')==chat_id and state.get('until',0)>time.time()
                except Exception:pending_compose=False
            import cr_screenshots
            if cr_screenshots.group_request(msg,out):return
            if GA.action_request(text) or pending_compose:
                if uid<=0 or msg.get('sender_chat') or any(msg.get(k) for k in ('forward_origin','forward_from','via_bot')):
                    out.send(chat_id,'Account actions need your direct request, not an anonymous or forwarded message.');return
                if re.search(r'(?i)\b(connect|link|reconnect|enable)\b.*\b(google|gmail|calendar booking)\b',text) or text.startswith('/connect_google'):
                    out.send(chat_id,'Connect Google in a private DM first so your account link is not exposed here.');return
                if not GA.consent(uid,chat_id,text,out):return
                groupout=GA.GroupOut(out,uid,chat_id)
                with mem.user_lock(uid):_handle_text(uid,chat_id,name,text,msg.get('message_id'),groupout)
                return
            progress=ProgressOut(out,chat_id,((12,"On it. Give me a moment."),(25,"Still working on it. I'll send the answer when it's ready."))).start()
            try:
                out.typing(chat_id)
                reply=cr_group.answer(msg)
                progress.send(chat_id,reply,markup=getattr(reply,'markup',None))
            finally:progress.stop()
            return
        if uid > 0:
            db.kv_set("tg_latest_"+str(uid), {"chat_id":chat_id,"message_id":msg.get("message_id"),
                "media":any(msg.get(k) for k in ("photo","voice","audio","document","video","video_note","animation","sticker"))})
        text = msg.get("text")
        if text and (text.startswith(("/email_send", "/email_draft", "/connect_google")) or __import__("re").search(r"\b(email|gmail|inbox|mail|calendar|send|google|workspace|github|sheet|doc)\b",text,__import__("re").I)) and (msg.get("forward_origin") or msg.get("forward_from") or msg.get("via_bot")):
            out.send(chat_id, "Google actions need a direct command from you, not forwarded content.")
            return
        if any(msg.get(k) for k in ("photo","voice","audio","document","video","video_note","animation","sticker")) or not text:
            with mem.user_lock(uid):
                _handle_media(uid, chat_id, name, msg, out)
            return
        with mem.user_lock(uid):
            progress=ProgressOut(out,chat_id,((12,"On it. Give me a moment."),(25,"Still working on it. I'll send the answer when it's ready."),(35,"This is taking longer than usual. I'm still checking."))).start()
            try:
                import cr_reply_context as R
                token=R.current.set(R.telegram(msg))
                try:__import__('cr_history').run(uid, chat_id, name, text, msg.get("message_id"), progress, _handle_text)
                finally:R.current.reset(token)
            finally:progress.stop()
    except Exception as e:
        log.exception("handle_update failed")
        try:
            out.send(upd.get("message", {}).get("chat", {}).get("id"), "Something went wrong on my side and I couldn't finish that. Nothing was changed. Try again in a moment.")
        except Exception:
            pass


def msg_private_invalid(uid):
    return uid <= 0


def _handle_text(uid, chat_id, name, text, message_id, out):
    if looks_like_secret(text):
        out.delete(chat_id, message_id)
        out.send(chat_id, "That looked like a password or key, so I deleted your message and did not save it. Don't paste secrets here. If it was real, rotate it.")
        db.audit(uid, "secret_blocked")
        return
    import cr_reactions
    emoji = cr_reactions.choose(text)
    if emoji and message_id and hasattr(out, "react"):
        reaction_ok = out.react(chat_id, message_id, emoji)
        db.audit(uid,"telegram_reaction",f"message_id={message_id} emoji={emoji} accepted={reaction_ok}")
    if text.strip().lower().rstrip('.!') in ('hi','hey','hello','cool','thanks','thank you'):
        out.send(chat_id,"You're welcome." if text.strip().lower().rstrip('.!') in ('thanks','thank you') else "Hey! What can I help with?" if text.strip().lower().rstrip('.!') in ('hi','hey','hello') else "Got it.");return
    text=__import__('cr_natural').translate(text)
    if __import__('cr_followups').handle(uid,chat_id,text,out):return
    if __import__('cr_plugins').handle(uid,chat_id,text,out):return
    if __import__('cr_persona').handle(uid,chat_id,text,out):return
    if __import__('cr_voice').handle(uid,chat_id,text,out):return
    if __import__('cr_games').handle(uid,chat_id,text,out):return
    if chat_id<0 and __import__('re').search(r'(?i)(?:email checks|daily check-ins|(?:morning|evening) digest|/proactive|/digest)',text):
        out.send(chat_id,'Set up private monitoring and proactive updates in a DM. Group requests do not move your background alerts here.');return
    import cr_connections as X
    import re
    conn=re.fullmatch(r'(?i)(connect|disconnect|status) (workspace|github)',text.strip())
    if conn:
        if chat_id!=uid:out.send(chat_id,'Connection links/status stay in your private DM.');return
        try:
            op,provider=conn.group(1).lower(),conn.group(2).lower()
            if op=='connect':out.send(chat_id,'Connect your own '+provider+' account. Workspace is separate from Gmail/calendar. Review provider permissions and choose the intended account:\n'+X.begin(uid,provider))
            elif op=='status':
                row=X.status(uid,provider);out.send(chat_id,provider+': '+row['identity'] if row else 'Not connected: '+provider)
            else:out.send(chat_id,'Local '+provider+' credentials removed. '+('Provider revocation confirmed.' if X.disconnect(uid,provider) else 'Remove access in the provider account too.'))
        except Exception:out.send(chat_id,'Connection unavailable or expired; no account change confirmed.')
        return
    ws=re.fullmatch(r'(?i)(?:read sheet|read doc) ([A-Za-z0-9_-]{15,150})(?: (.+))?',text.strip())
    if ws:
        if chat_id!=uid:out.send(chat_id,'Workspace data stays in your private DM in this beta.');return
        try:
            import cr_workspace as W
            if text.lower().startswith('read sheet'):
                result=W.sheet_read(uid,ws.group(1),ws.group(2) or 'A1:J20')
                out.send(chat_id,'Sheet '+result.get('range','')+'\n'+__import__('json').dumps(result.get('values',[]),ensure_ascii=False))
            else:
                result=W.doc_read(uid,ws.group(1));out.send(chat_id,result['title']+'\n\n'+result['text'])
        except Exception:out.send(chat_id,'Workspace read not confirmed. Check your Workspace connection, file permission and bounded range.')
        return
    gh=re.fullmatch(r'(?i)github digest ([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)',text.strip())
    if gh:
        if chat_id!=uid:out.send(chat_id,'GitHub digests currently stay in your DM.');return
        try:
            import cr_github
            result=cr_github.digest(uid,gh.group(1))
            out.send(chat_id,result['repo']+'\n'+result['url']+'\n\n'+'\n\n'.join(str(x['type'])+' '+str(x['id'])+': '+x['title']+'\n'+x['url'] for x in result['items']))
        except Exception:out.send(chat_id,'No GitHub digest confirmed. Check public owner/repo or try after the rate limit clears.')
        return
    if text.strip().lower() in ('/rooms','shared rooms'):
        out.send(chat_id,'Telegram shared conversations use your Telegram groups: add @crayon_v1_bot and tag it for public questions. Personal group actions require each member to opt in and review before sharing. Web shared rooms are in Menu > Shared rooms at https://itsppm76.github.io/crayon/ with explicit invite/audience review. No private history is imported and Telegram groups are not silently bridged to web rooms.');return
    if __import__('cr_workspace_create').handle(uid,chat_id,text,out):return
    import cr_workspace_review
    if cr_workspace_review.handle(uid,chat_id,text,out):return
    import cr_booking
    if cr_booking.handle(uid,chat_id,text,out):return
    import cr_dashboard
    if cr_dashboard.handle(uid,chat_id,text,out):return
    import cr_work
    if cr_work.handle(uid,chat_id,text,out):return
    plain=text.strip().lower().rstrip('.!')
    aliases={"help":"/help","connect google":"/connect_google","disconnect google":"/disconnect_google","google status":"/google_status","what do you remember about me?":"/memory","what do you remember about me":"/memory","show my memory":"/memory","memory review":"/memory_review","turn on daily check-ins":"/proactive on","turn off daily check-ins":"/proactive off","morning digest":"/digest morning","evening digest":"/digest evening","turn off digests":"/digest off","my digest":"/digest_now"}
    if plain=="privacy options":
        out.send(chat_id,"You can say 'show my memory' to review saved facts, or ask me to forget a specific fact. To remove all stored personal data and Google access, use the delete option below.",markup={"inline_keyboard":[[{"text":"Review memory","callback_data":"ux:memory"},{"text":"Delete my data","callback_data":"ux:delete_review"}]]});return
    if __import__('re').fullmatch(r"(?:(?:please|plz|pls) )?(?:connect|link|reconnect)(?: to)?(?: my)? (?:google|gmail)(?: account)?(?: please)?[.!?]*",plain) or plain in ('how do i connect google','how to connect google','i want to connect google','connect my google account'):
        plain='connect google'
    text=aliases.get(plain,text)
    if (not text.startswith('/') or text.startswith('/calendar_slot')) and (chat_id==uid or chat_id<0):
        import cr_google_chat
        if cr_google_chat.handle(uid,chat_id,text,None,out):return
    fields = text.split(None,1)
    if not fields:return
    cmd, arg = fields[0], fields[1] if len(fields)>1 else ""
    cmd = cmd.split("@")[0].lower()
    arg = arg.strip()
    if cmd in ('/browse','/computer','/research','/files'):
        examples={'/browse':'Visit https://www.instagram.com and send a screenshot. Public sites only; no account login.', '/computer':'On my computer calculate 20*(3+2)/4. Files stay in your private account folder.', '/research':'Go deep on a comparison of Notion and Obsidian for student notes.', '/files':'Create a CSV and chart using Maths3, Finance5, Strategy2 hours.'}
        if not arg:out.send(chat_id,examples[cmd]);return
        text={'/browse':'Browser screenshot: ','/computer':'On my computer ','/research':'Research ','/files':'Create CSV/chart: '}[cmd]+arg
        reply,meta=A.respond(uid,chat_id,text,name)
        out.send(chat_id,reply)
        for item in meta.get('artifacts',[]):out.artifact(chat_id,item)
        return
    if cmd=='/email_checks':
        import cr_mail_watch as W,cr_google as G
        try:
            if arg=='status':out.send(chat_id,W.status(uid));return
            if arg not in ('on','off'):out.send(chat_id,'Use /email_checks on, off or status. Owner beta, hourly metadata only, quiet hours respected.');return
            out.send(chat_id,W.configure(uid,chat_id,arg=='on'))
        except G.GoogleError as e:out.send(chat_id,str(e))
        return
    if cmd == "/start":
        mem.touch_user(uid, name)
        out.send(chat_id, f"Hi{' ' + name if name else ''}, I'm Crayon. I remember what matters about you now, even after restarts. Just tell me what you need or say help. Use the menu for mail, calendar previews, research, charts, memory and updates. Browser/computer use bounded shared capacity; files are private per account. Connect Google in your own private chat.")
    elif cmd == "/help":
        out.send(chat_id, HELP, markup={"keyboard":[[{"text":"My reminders"},{"text":"My tasks"}],[{"text":"Show my memory"},{"text":"Privacy options"}],[{"text":"Connect Google"},{"text":"Help"}]],"resize_keyboard":True,"one_time_keyboard":True})
    elif cmd == "/goal":
        if not arg:
            out.send(chat_id, "Use /goal followed by a concrete research or calculation goal.")
        elif _over_cap(uid):
            out.send(chat_id, "Today's free-tier message limit is reached.")
        else:
            out.typing(chat_id)
            reply, meta = A.respond(uid, chat_id, arg, name, goal_mode=True)
            if hasattr(out, "meta"):
                out.meta = meta
            out.send(chat_id, reply)
    elif cmd in ("/connect_google", "/disconnect_google", "/google_status", "/gmail", "/gmail_read", "/calendar", "/email_draft", "/email_send", "/email_cancel"):
        import cr_google as G
        if msg_private_invalid(uid) or (chat_id!=uid and chat_id>=0):
            out.send(chat_id, "Google commands only work in your private chat with Crayon.")
            return
        try:
            if cmd != "/disconnect_google" and not G.configured():
                raise G.GoogleError("Google setup is not active yet")
            if cmd == "/connect_google":
                out.send(chat_id, "Open this link to connect your own Google account:\n" + G.begin(uid) + "\n\nThis link is only for your Telegram account. Don't forward it. Friends must type 'connect Google' in their own private Crayon chat. Choose your whitelisted Google email, then review Google's permissions. This link works even if you don't see a Connect Google button.\n\nCrayon is in testing mode: only approved tester emails can connect, and access may need renewing after 7 days. Mail/calendar results stay in this private chat and aren't sent to the AI model or permanent memory. Tokens are encrypted. I show each email draft before you approve sending. Say enable calendar booking for an optional calendar-write reconnect, or turn on email checks for hourly metadata-only checks of your own account during awake hours.\n\nPrivacy: " + C.PUBLIC_URL + "/privacy\nYou can always type 'connect Google' or /connect_google to get a fresh link.")
            elif cmd == "/disconnect_google":
                r=G.disconnect(uid)
                out.send(chat_id, "Stored Google credentials and pending drafts removed. " + ("Google revocation confirmed." if r["revoked"] else "Google revocation was not confirmed; remove Crayon access in your Google account too."))
            elif cmd == "/google_status":
                r=G.status(uid)
                out.send(chat_id, "Connected Google account: " + r["email"] if r else "No Google account connected.")
            elif cmd == "/gmail":
                out.send(chat_id,G.inbox(uid,arg))
                db.kv_set('google_mail_results_chat_'+str(uid),chat_id)
            elif cmd == "/gmail_read":
                if chat_id!=uid and db.kv_get('google_mail_results_chat_'+str(uid),uid)!=chat_id:
                    out.send(chat_id,'Check your mail in this chat first.');return
                out.send(chat_id,G.read_message(uid,arg))
            elif cmd == "/calendar":
                out.send(chat_id,G.calendar(uid))
            elif cmd == "/email_draft":
                import cr_google_chat
                cr_google_chat.show_draft(uid,chat_id,out,arg)
            elif cmd == "/email_send":
                if db.kv_get('google_review_chat_'+str(uid),uid)!=chat_id:
                    out.send(chat_id,'Review your draft in this chat before confirming it.');return
                bits=arg.split()
                if len(bits)!=2:raise G.GoogleError("Use the exact /email_send command shown beneath your draft")
                out.send(chat_id,G.send_draft(uid,*bits))
            elif cmd == "/email_cancel":
                out.send(chat_id,G.cancel_draft(uid,arg))
        except G.GoogleError as e:
            out.send(chat_id,str(e))
        except Exception:
            out.send(chat_id,"Google request failed. No action is confirmed. Try reconnecting if access expired.")
    elif cmd == "/memory_review":
        import json
        mem.touch_user(uid, name)
        review = mem.review_memory(uid)
        out.send(chat_id, "Memory review (no facts changed):\n" + json.dumps(review, ensure_ascii=False, indent=2))
        if hasattr(out, "meta"):
            out.meta = {"memory_review": review}
    elif cmd in ("/proactive", "/digest", "/quiet_hours"):
        import cr_proactive as P
        if cmd == "/quiet_hours":
            try:
                start, end = [int(x) for x in arg.split()]
                if not (0 <= start <= 23 and 0 <= end <= 23):
                    raise ValueError()
                P.set_option(uid, chat_id, "quiet_start", start)
                ok = P.set_option(uid, chat_id, "quiet_end", end)
                out.send(chat_id, f"Quiet hours: {start}:00 to {end}:00 in your timezone." if ok else "Couldn't save quiet hours.")
            except Exception:
                out.send(chat_id, "Use /quiet_hours <start hour> <end hour>, e.g. /quiet_hours 21 9.")
        else:
            key = "proactive" if cmd == "/proactive" else "digest"
            allowed = ("on", "off") if key == "proactive" else ("morning", "evening", "both", "off")
            if arg not in allowed:
                out.send(chat_id, "Use " + cmd + " " + "|".join(allowed) + ". Currently: " + str(P.settings(uid)[key]))
            else:
                value = (arg == "on") if key == "proactive" else arg
                ok = P.set_option(uid, chat_id, key, value)
                out.send(chat_id, cmd[1:] + " is " + arg + ". Quiet hours apply; free-host timing is best-effort." if ok else "Couldn't save that setting.")
    elif cmd == "/digest_now":
        import cr_proactive as P
        mem.touch_user(uid, name)
        out.send(chat_id, P.digest_text(uid))
    elif cmd == "/memory":
        out.send(chat_id, mem.render_memory(uid))
    elif cmd == "/forget":
        if not arg:
            out.send(chat_id, "Which one? Use /memory to see keys, then /forget <key>.")
        else:
            ok = mem.forget(uid, arg)
            out.send(chat_id, f"Removed '{arg}'." if ok else f"I tried to remove '{arg}' but it's still there. Not confirmed.")
    elif cmd == "/delete_my_data":
        if arg.lower() == "confirm":
            ok = mem.delete_all(uid)
            out.send(chat_id, "Everything I held on you is deleted (checked: nothing left)." if ok else "I tried to wipe your data but some rows remain. Not fully deleted.")
        else:
            out.send(chat_id,"This permanently deletes memory, notes, reminders, chat history and Google credentials.",markup={"inline_keyboard":[[{"text":"Review deletion","callback_data":"ux:delete_review"}]]})
    elif text.startswith("/"):
        out.send(chat_id, "I don't recognize that. Tell me what you want to do, or say help.")
    else:
        if _over_cap(uid):
            out.send(chat_id, "You've hit today's message limit for the free tier. Try again tomorrow.")
            return
        out.typing(chat_id)
        reply, meta = A.respond(uid, chat_id, text, name)
        if hasattr(out, "meta"):
            out.meta = meta
        out.send(chat_id, reply)

        for item in meta.pop('artifacts',[]):
            try:
                ident=out.artifact(chat_id,item)
                db.audit(uid,'attachment_sent',item['filename']+' message='+str(ident))
            except Exception as e:
                log.warning('attachment failed: %s',redact(str(e))[:180])
                out.send(chat_id,"I made the file, but Telegram delivery wasn't confirmed. Don't count on receiving it.")


def handle_callback(cb, out):
    try:
        api("answerCallbackQuery", callback_query_id=cb["id"])
    except Exception:
        pass
    data=cb.get("data","")
    if not data.startswith(("email_send:","email_cancel:","calendar_create:","calendar_cancel:","form_submit:","form_cancel:","sheet:","workspace_create:","ux:","work:")):return
    uid=cb.get("from",{}).get("id",0)
    chat_id=cb.get("message",{}).get("chat",{}).get("id")
    if uid<=0:return
    if chat_id!=uid:
        import cr_group_actions as GA
        if not isinstance(chat_id,int) or chat_id>=0 or not GA.reviewed(uid,chat_id,data):return
        out=GA.GroupOut(out,uid,chat_id)
    if data.startswith('workspace_create:'):
        try:
            parts=data.split(':')
            with mem.user_lock(uid):out.send(chat_id,__import__('cr_workspace_create').confirm_chat(uid,chat_id,parts[2],parts[3],parts[1]))
        except Exception as e:out.send(chat_id,str(e)[:300])
        return
    if data.startswith('sheet:'):
        parts=data.split(':')
        if len(parts)!=4:return
        try:
            with mem.user_lock(uid):out.send(chat_id,__import__('cr_workspace_review').confirm(uid,chat_id,parts[2],parts[3],parts[1]))
        except ValueError as e:out.send(chat_id,str(e))
        return
    if data.startswith(('form_submit:','form_cancel:')):
        import cr_booking as B,base64
        parts=data.split(':')
        try:
            with mem.user_lock(uid):
                if parts[0]=='form_cancel' and len(parts)==2:out.send(chat_id,B.cancel(uid,parts[1]))
                elif parts[0]=='form_submit' and len(parts)==3:
                    r=B.submit(uid,parts[1],parts[2]);out.send(chat_id,r.get('note','Outcome not confirmed')+'\n'+r.get('url',''))
                    if r.get('screenshot'):out.artifact(chat_id,{'filename':'form-result.png','mime':'image/png','data':base64.b64decode(r['screenshot'],validate=True)})
        except B.BookingError as e:out.send(chat_id,str(e))
        return
    if data.startswith("work:"):
        import re,cr_work
        if not re.fullmatch(r'work:(show|pause|resume|cancel|export):[1-9][0-9]{0,10}',data):return
        _,op,ident=data.split(':')
        with mem.user_lock(uid):cr_work.handle(uid,chat_id,'/work '+op+' '+ident,out)
        return
    if data.startswith("ux:"):
        with mem.user_lock(uid):
            if data=="ux:delete_review":out.send(chat_id,"Permanently delete all stored memory, notes, reminders, chat history and Google credentials?",markup={"inline_keyboard":[[{"text":"Delete all my data","callback_data":"ux:delete_confirm"},{"text":"Keep my data","callback_data":"ux:keep"}]]})
            elif data=="ux:delete_confirm":_handle_text(uid,chat_id,"","/delete_my_data confirm",None,out)
            elif data=="ux:memory":_handle_text(uid,chat_id,"","/memory",None,out)
            else:out.send(chat_id,"Kept your data unchanged.")
        return
    import cr_google as G
    try:
        with mem.user_lock(uid):
            parts=data.split(":")
            if parts[0]=='calendar_create' and len(parts)==3:
                import cr_calendar_draft as K
                out.send(chat_id,K.create(uid,parts[1],parts[2]))
            elif parts[0]=='calendar_cancel' and len(parts)==2:
                import cr_calendar_draft as K
                out.send(chat_id,K.cancel(uid,parts[1]))
            elif parts[0]=="email_send" and len(parts)==3:
                out.send(chat_id,G.send_draft(uid,parts[1],parts[2]))
            elif parts[0]=="email_cancel" and len(parts)==2:
                out.send(chat_id,G.cancel_draft(uid,parts[1]))
    except G.GoogleError as e:out.send(chat_id,str(e))
    except Exception:out.send(chat_id,"Google action not confirmed. Check Gmail Sent or Calendar before retrying.")


def set_commands():
    try:
        api("setMyCommands", commands=[
            {"command":"help","description":"See what I can do"},
            {"command":"tasks","description":"Task dashboard and next steps"},
            {"command":"work","description":"Bounded background research and maths"},
            {"command":"connect_google","description":"Connect your own Gmail and calendar"},
            {"command":"google_status","description":"Check your Google connection"},
            {"command":"gmail","description":"Check your mail"},
            {"command":"calendar","description":"See the next week on your calendar"},
            {"command":"calendar_slot","description":"Preview a private calendar slot before booking"},
            {"command":"email_checks","description":"Private hourly mail checks on/off"},
            {"command":"rooms","description":"Shared rooms and Telegram group guidance"},
            {"command":"browse","description":"Public website screenshot (per-user limits)"},
            {"command":"computer","description":"Computer status, maths and private files"},
            {"command":"research","description":"Research a topic with sources"},
            {"command":"files","description":"Create a CSV or chart from your data"},
            {"command":"memory","description":"See what I remember about you"},
            {"command":"forget","description":"Remove one remembered fact"},
            {"command":"digest","description":"Set morning or evening updates"},
            {"command":"quiet_hours","description":"Set hours with no proactive pings"},
            {"command":"disconnect_google","description":"Remove your Google connection"},
            {"command":"delete_my_data","description":"Delete your stored data after confirmation"}])
    except Exception as e:
        log.warning("setMyCommands failed: %s", redact(str(e)))


def poll_forever(stop=None):
    offset = 0
    try:
        offset = int(db.kv_get("tg_offset", 0) or 0)
    except Exception:
        pass
    try:
        api("deleteWebhook", drop_pending_updates=False)
    except Exception as e:
        log.warning("deleteWebhook: %s", redact(str(e)))
    log.info("polling started at offset %s", offset)
    while not (stop and stop.is_set()):
        try:
            r = _http.post(f"https://api.telegram.org/bot{C.TELEGRAM_TOKEN}/getUpdates",
                           json={"offset": offset, "timeout": 25, "allowed_updates": ["message", "callback_query"]}, timeout=40)
            data = r.json()
            if not data.get("ok"):
                if r.status_code == 409:
                    time.sleep(6)  # another instance polling during a deploy
                else:
                    log.warning("getUpdates: %s", redact(str(data.get("description"))))
                    time.sleep(3)
                continue
            for upd in data["result"]:
                offset = upd["update_id"] + 1
                _pool.submit(handle_update, upd)
            if data["result"]:
                try:
                    db.kv_set("tg_offset", offset)
                except Exception:
                    pass
        except Exception as e:
            log.warning("poll error: %s", redact(f"{type(e).__name__}: {e}")[:200])
            time.sleep(3)


def _handle_media(uid, chat_id, name, msg, out):
    import cr_media
    item, mime = None, ""
    if msg.get("photo"):
        item, mime = msg["photo"][-1], "image/jpeg"
    elif msg.get("voice"):
        item, mime = msg["voice"], "audio/ogg"
    elif msg.get("audio"):
        item = msg["audio"]
        mime = item.get("mime_type", "audio/mpeg")
    elif msg.get("document"):
        item = msg["document"]
        mime = item.get("mime_type", "")
    else:
        for kind in ("video", "video_note", "animation", "sticker"):
            if msg.get(kind):
                item = msg[kind]
                mime = item.get("mime_type") or ("video/webm" if item.get("is_video") else "application/x-tgsticker" if item.get("is_animated") else "image/webp" if kind=="sticker" else "video/mp4")
                break
    if not item:
        out.send(chat_id, "I received an attachment type I cannot read yet. Try sending it as a document.")
        return
    caption = msg.get("caption", "")
    if looks_like_secret(caption):
        out.delete(chat_id, msg.get("message_id"))
        out.send(chat_id, "The caption appears to contain a secret. I did not process or save it.")
        return
    guard=ProgressOut(out,chat_id,((25,"Still reading it. Larger files can take a little longer."),(25,"Still processing with the media provider. I will tell you if it fails.")))
    progress=guard.emit
    try:
        mem.touch_user(uid, name)
        if _over_cap(uid):
            out.send(chat_id, "Today's free-tier message limit is reached.")
            return
        if item.get("file_size", 0) > cr_media.MAX_BYTES:
            raise ValueError("Telegram bot downloads are limited to 20 MB. Send a smaller file or split it.")
        if msg.get("message_id") and hasattr(out, "react"):
            accepted=out.react(chat_id,msg["message_id"],"👀")
            db.audit(uid,"telegram_reaction",f"message_id={msg['message_id']} emoji=👀 accepted={accepted}")
        progress("Big file, give me a moment to read it." if item.get("file_size", 0)>4_000_000 else "Got it, reading your file now.")
        guard.start()
        out.typing(chat_id)
        f = api("getFile", file_id=item["file_id"])
        path = f.get("file_path", "")
        if not path or ".." in path or path.startswith("/"):
            raise ValueError("Could not get the upload safely.")
        if f.get("file_size", 0) > cr_media.MAX_BYTES:
            raise ValueError("Upload exceeds the 20 MB download limit.")
        data = bytearray()
        with _http.stream("GET", f"https://api.telegram.org/file/bot{C.TELEGRAM_TOKEN}/{path}") as r:
            r.raise_for_status()
            for chunk in r.iter_bytes():
                data.extend(chunk)
                if len(data) > cr_media.MAX_BYTES:
                    raise ValueError("Upload exceeds the 20 MB download limit.")
        reply = cr_media.analyze(bytes(data), mime, caption, item.get("file_name", ""), progress=progress)
        guard.stop()
        db.audit(uid,"media_processed", f"mime={mime} bytes={len(data)}")
        mem.add_message(uid, "user", "[User sent media: " + mime + "] " + redact(caption))
        mem.add_message(uid, "assistant", "[Media analysis summary; raw file not retained] " + clean_text(reply)[:3000])
        if hasattr(out, "meta"):
            out.meta = {"media": mime, "bytes": len(data), "verified": bool(reply)}
        guard.send(chat_id, reply or "I couldn't read that clearly.")
    except Exception as e:
        guard.stop()
        log.warning("media handling failed: %s",type(e).__name__)
        reason = str(e) if isinstance(e, ValueError) else "The download or media provider failed. Try a smaller file or retry shortly."
        out.send(chat_id, "I couldn't read that upload: " + redact(reason)[:200])
    finally:
        guard.stop()
