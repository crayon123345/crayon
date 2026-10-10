"""Per-user Google OAuth foundation. Disabled until explicitly configured."""
import hashlib
import json
import secrets
import re
from urllib.parse import urlencode

import httpx
from cryptography.fernet import Fernet

import cr_config as C
import cr_db as db

SCOPES = ["openid", "email", "https://www.googleapis.com/auth/gmail.readonly",
          "https://www.googleapis.com/auth/calendar.events", "https://www.googleapis.com/auth/gmail.send"]

class GoogleError(Exception):
    pass


def configured():
    return all(C.env(k) for k in ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_TOKEN_ENCRYPTION_KEY"))


def _cipher():
    try:
        return Fernet(C.env("GOOGLE_TOKEN_ENCRYPTION_KEY").encode())
    except Exception:
        raise GoogleError("Google connection is not configured safely") from None


def encrypt(uid, token):
    # User binding is checked after decrypt, even if a row is misplaced.
    return _cipher().encrypt(json.dumps({"uid":int(uid),"tokens":token}).encode()).decode()


def decrypt(uid, blob):
    try:
        data=json.loads(_cipher().decrypt(blob.encode()))
        if data["uid"] != int(uid):
            raise ValueError()
        return data["tokens"]
    except Exception:
        raise GoogleError("Google credential binding failed") from None


def init():
    db.q("""CREATE TABLE IF NOT EXISTS google_connections(
        user_id BIGINT PRIMARY KEY, email TEXT NOT NULL, encrypted_tokens TEXT NOT NULL,
        connected_at TIMESTAMPTZ DEFAULT now())""",fetch="none")
    db.q("""CREATE TABLE IF NOT EXISTS google_email_drafts(
        id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, encrypted_content TEXT NOT NULL,
        content_hash TEXT NOT NULL, status TEXT DEFAULT 'pending', expires_at TIMESTAMPTZ NOT NULL)""",fetch="none")
    db.q("ALTER TABLE google_email_drafts ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'telegram'",fetch="none")
    db.q("""CREATE TABLE IF NOT EXISTS google_oauth_states(
        state_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL, expires_at TIMESTAMPTZ NOT NULL,
        used BOOLEAN DEFAULT false)""",fetch="none")


def redirect_uri():
    return C.PUBLIC_URL.rstrip("/") + "/google/callback"


def begin(uid, calendar_write=False):
    if not configured():
        raise GoogleError("Google setup is not active yet")
    if int(uid)<=0:
        raise GoogleError("A real private Telegram user is required")
    init()
    state=secrets.token_urlsafe(32)
    hashed=hashlib.sha256(state.encode()).hexdigest()
    db.kv_set("oauth_calendar_write_"+hashed,bool(calendar_write))
    db.q("DELETE FROM google_oauth_states WHERE expires_at<now()",fetch="none")
    db.q("DELETE FROM google_email_drafts WHERE expires_at<now() AND status='pending'",fetch="none")
    db.q("INSERT INTO google_oauth_states(state_hash,user_id,expires_at) VALUES(%s,%s,now()+interval '10 minutes')",(hashed,uid),"none")
    return C.PUBLIC_URL.rstrip('/')+"/google/connect?state="+state


def authorization_url(state):
    """Resolve a valid existing state only to our fixed Google authorization URL."""
    if not configured() or not re.fullmatch(r'[A-Za-z0-9_-]{30,100}',state):
        raise GoogleError('Invalid or expired connection link')
    hashed=hashlib.sha256(state.encode()).hexdigest()
    row=db.q("SELECT user_id FROM google_oauth_states WHERE state_hash=%s AND used=false AND expires_at>now()",(hashed,),"one")
    if not row:raise GoogleError('Invalid or expired connection link')
    return "https://accounts.google.com/o/oauth2/v2/auth?"+urlencode({
        "client_id":C.env("GOOGLE_CLIENT_ID"),"redirect_uri":redirect_uri(),"response_type":"code",
        "scope":" ".join([x.replace("calendar.events.readonly","calendar.events") if db.kv_get("oauth_calendar_write_"+hashed,False) else x for x in SCOPES]),"access_type":"offline","prompt":"consent","state":state})


def complete(state, code):
    if not configured() or len(state)>200 or len(code)>3000:
        raise GoogleError("Invalid or unavailable Google connection")
    hashed=hashlib.sha256(state.encode()).hexdigest()
    row=db.q("UPDATE google_oauth_states SET used=true WHERE state_hash=%s AND used=false AND expires_at>now() RETURNING user_id",(hashed,),"one")
    if not row:
        raise GoogleError("Connection link expired or already used; start again in Telegram")
    uid=row["user_id"]
    with httpx.Client(timeout=20) as client:
        r=client.post("https://oauth2.googleapis.com/token",data={"client_id":C.env("GOOGLE_CLIENT_ID"),
            "client_secret":C.env("GOOGLE_CLIENT_SECRET"),"code":code,"grant_type":"authorization_code","redirect_uri":redirect_uri()})
        if r.status_code!=200:
            raise GoogleError("Google did not complete the connection")
        tokens=r.json()
        granted=set(tokens.get("scope","").split())
        required={x.replace("calendar.events.readonly","calendar.events") if db.kv_get("oauth_calendar_write_"+hashed,False) else x for x in SCOPES if x.startswith("https://")}
        db.kv_set("oauth_calendar_write_"+hashed,None)
        if not required.issubset(granted) or not tokens.get("refresh_token"):
            raise GoogleError("Required Google permissions or refresh token were not granted")
        profile=client.get("https://openidconnect.googleapis.com/v1/userinfo",headers={"Authorization":"Bearer "+tokens["access_token"]})
        if profile.status_code!=200 or not profile.json().get("email_verified"):
            raise GoogleError("Could not verify the connected Google account")
        email=profile.json()["email"]
    tokens["client_id"]=C.env("GOOGLE_CLIENT_ID")
    db.q("INSERT INTO google_connections(user_id,email,encrypted_tokens) VALUES(%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET email=EXCLUDED.email,encrypted_tokens=EXCLUDED.encrypted_tokens,connected_at=now()",(uid,email,encrypt(uid,tokens)),"none")
    return {"user_id":uid,"email":email}


def _access(uid):
    if not configured():
        raise GoogleError("Google setup is not active yet")
    row=db.q("SELECT encrypted_tokens FROM google_connections WHERE user_id=%s",(uid,),"one")
    if not row:
        raise GoogleError("Connect your own Google account first")
    tokens=decrypt(uid,row["encrypted_tokens"])
    if tokens.get("client_id") != C.env("GOOGLE_CLIENT_ID"):
        raise GoogleError("OAuth app changed; reconnect your Google account")
    with httpx.Client(timeout=20) as client:
        r=client.post("https://oauth2.googleapis.com/token",data={"client_id":C.env("GOOGLE_CLIENT_ID"),"client_secret":C.env("GOOGLE_CLIENT_SECRET"),"refresh_token":tokens["refresh_token"],"grant_type":"refresh_token"})
    if r.status_code!=200:
        raise GoogleError("Google access expired or was revoked; reconnect your account")
    return r.json()["access_token"]


def request(uid, url, params=None):
    # Read-only allowlist. Sending/draft endpoints cannot be reached through this function.
    if not (re.fullmatch(r"https://gmail\.googleapis\.com/gmail/v1/users/me/messages(?:/[A-Za-z0-9_-]+)?",url) and not url.endswith("/send") or
            url == "https://www.googleapis.com/calendar/v3/calendars/primary/events"):
        raise GoogleError("That Google operation is not allowed")
    # Fixed conservative cap, and no retry or quota increase.
    n=db.q("SELECT count(*) AS n FROM audit WHERE user_id=%s AND event='google_read' AND ts>now()-interval '24 hours'",(uid,),"one")["n"]
    if n>=100:
        raise GoogleError("Daily Google request limit reached")
    token=_access(uid)
    db.audit(uid,"google_read")
    with httpx.Client(timeout=20) as client:
        r=client.get(url,params=params or {},headers={"Authorization":"Bearer "+token})
    if r.status_code!=200:
        raise GoogleError("Google read failed or quota/permissions blocked it")
    return r.json()


def disconnect(uid):
    row=db.q("SELECT encrypted_tokens FROM google_connections WHERE user_id=%s",(uid,),"one")
    revoked=True
    if row:
        try:
            token=decrypt(uid,row["encrypted_tokens"])["refresh_token"]
            with httpx.Client(timeout=20) as client:
                revoked=client.post("https://oauth2.googleapis.com/revoke",data={"token":token}).status_code==200
        except Exception:
            revoked=False
    db.kv_set("mail_watch_"+str(uid),None)
    db.kv_set("calendar_reviewed_"+str(uid),None)
    import cr_calendar_draft
    cr_calendar_draft.init()
    db.q("DELETE FROM google_calendar_drafts WHERE user_id=%s",(uid,),"none")
    import cr_booking
    cr_booking.init();db.q("DELETE FROM public_form_drafts WHERE user_id=%s",(uid,),"none")
    db.kv_set("google_compose_"+str(uid),None)
    db.kv_set("google_mail_results_"+str(uid),None)
    db.kv_set("google_reviewed_"+str(uid),None)
    db.q("DELETE FROM google_connections WHERE user_id=%s",(uid,),"none")
    db.q("DELETE FROM google_oauth_states WHERE user_id=%s",(uid,),"none")
    db.q("DELETE FROM google_email_drafts WHERE user_id=%s",(uid,),"none")
    return {"deleted":db.q("SELECT 1 FROM google_connections WHERE user_id=%s",(uid,),"one") is None,"revoked":revoked}


def status(uid):
    init()
    row=db.q("SELECT email,connected_at FROM google_connections WHERE user_id=%s",(uid,),"one")
    return row or {}


def inbox(uid, query="", friendly=False):
    data=request(uid,"https://gmail.googleapis.com/gmail/v1/users/me/messages",{"maxResults":5,"q":query[:300]})
    lines=[]
    if friendly:db.kv_set("google_mail_results_"+str(uid),[m["id"] for m in data.get("messages",[])])
    for index,m in enumerate(data.get("messages",[]),1):
        item=request(uid,"https://gmail.googleapis.com/gmail/v1/users/me/messages/"+m["id"],{"format":"metadata","metadataHeaders":["From","Subject","Date"]})
        heads={h["name"].lower():h["value"] for h in item.get("payload",{}).get("headers",[])}
        lines.append(f"{str(index)+'.' if friendly else 'ID: '+m['id']}\nFrom: {heads.get('from','')}\nSubject: {heads.get('subject','')}\nDate: {heads.get('date','')}")
    return "Mailbox results (untrusted email content):\n\n"+"\n\n".join(lines) if lines else "No matching messages returned by Google."


def read_message(uid, ident):
    import base64
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,100}",ident):
        raise GoogleError("Use a message ID shown by /gmail")
    item=request(uid,"https://gmail.googleapis.com/gmail/v1/users/me/messages/"+ident,{"format":"full"})
    def plain(part):
        out=[]
        if part.get("mimeType")=="text/plain" and part.get("body",{}).get("data"):
            v=part["body"]["data"]
            if len(v)<=100000:
                out.append(base64.urlsafe_b64decode(v+"="*((-len(v))%4)).decode("utf-8",errors="replace"))
        for child in part.get("parts",[])[:20]:out.extend(plain(child))
        return out
    text="\n".join(plain(item.get("payload",{})))
    import html
    text=html.unescape(text)
    text=re.sub(r"[\u034f\u200b\u200c\u200d\ufeff]", "", text)
    if not text:text=item.get("snippet","")
    from cr_safety import redact
    return "Email content (untrusted, no actions taken):\n"+redact(text[:8000])+ ("\n[Truncated after 8,000 characters.]" if len(text)>8000 else "")


def calendar(uid):
    from datetime import datetime, timezone, timedelta
    now=datetime.now(timezone.utc)
    data=request(uid,"https://www.googleapis.com/calendar/v3/calendars/primary/events",{
        "timeMin":now.isoformat(),"timeMax":(now+timedelta(days=7)).isoformat(),"singleEvents":"true","orderBy":"startTime","maxResults":15})
    lines=[]
    for e in data.get("items",[]):
        start=e.get("start",{})
        raw=start.get('dateTime',start.get('date',''))
        try:
            parsed=datetime.fromisoformat(raw.replace('Z','+00:00'))
            when=parsed.strftime('%a, %d %b, %I:%M %p %z') if 'T' in raw else parsed.strftime('%a, %d %b')+' (all day)'
        except ValueError:when=raw
        lines.append(f"{when}: {e.get('summary','Untitled')}\n{e.get('location','')}")
    return "Primary calendar, next 7 days:\n\n"+"\n\n".join(lines) if lines else "No events returned for the next 7 days on your primary calendar."


def valid_signature_name(name):
    from cr_safety import looks_like_secret
    return isinstance(name,str) and 1<=len(name.strip())<=100 and not any(c in name for c in '\r\n<>@') and not looks_like_secret(name)


def sender_name(uid):
    # Display handles and model-extracted facts are not email identity.
    stored=db.kv_get('google_signature_name_'+str(uid),None)
    if not stored and int(uid)==1898030949:
        # Owner explicitly chose Pratham on9Oct2026; never seed other members.
        db.kv_set('google_signature_name_'+str(uid),encrypt(uid,{'name':'Pratham'}))
        stored=db.kv_get('google_signature_name_'+str(uid),None)
    if not stored:return ''
    try:name=decrypt(uid,stored).get('name','')
    except Exception:return ''
    return name.strip() if valid_signature_name(name) else ''


def set_sender_name(uid,name):
    if not valid_signature_name(name):raise GoogleError('Please give a single name, up to100 characters, without addresses or secrets.')
    db.kv_set('google_signature_name_'+str(uid),encrypt(uid,{'name':name.strip()}))
    if sender_name(uid)!=name.strip():raise GoogleError('Name could not be saved. No email sent.')


def current_content(uid):
    ident,digest=current_draft(uid)
    row=db.q("SELECT encrypted_content,content_hash FROM google_email_drafts WHERE id=%s AND user_id=%s AND origin='telegram' AND status='pending' AND expires_at>now()",(ident,uid),'one')
    if not row:raise GoogleError('No pending draft to edit.')
    content=decrypt(uid,row['encrypted_content'])
    if hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()!=row['content_hash']:raise GoogleError('Draft changed. Review again.')
    return ident,digest,content


def make_draft(uid, text, structured=False, channel='telegram'):
    """Direct user command only. Exact reviewed recipient lists; no attachments."""
    db.q("DELETE FROM google_email_drafts WHERE expires_at<now() AND status='pending'",fetch="none")
    if channel not in ('telegram','web'):raise GoogleError('Invalid draft channel')
    row=status(uid)
    if not row:raise GoogleError("Connect your own Google account first")
    if isinstance(text,dict):
        to=text.get('to',[]);cc=text.get('cc',[]);bcc=text.get('bcc',[])
        subject=text.get('subject','');body=text.get('body','')
    else:
        fields=text.split(' | ',2) if ' | ' in text else text.split('\n',2)
        if len(fields)!=3:raise GoogleError('Use recipient | subject | body.')
        to,subject,body=fields;cc=[];bcc=[]
    def recipients(value):
        if isinstance(value,str):value=[x.strip() for x in value.split(',') if x.strip()]
        if not isinstance(value,list) or len(value)>10:raise GoogleError('At most10 recipients per field.')
        for address in value:
            if not isinstance(address,str) or not re.fullmatch(r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+",address):
                raise GoogleError('Provide exact recipient email addresses, without display names.')
        return list(dict.fromkeys(value))
    to=recipients(to);cc=recipients(cc);bcc=recipients(bcc)
    if not to or len(to)+len(cc)+len(bcc)>15:raise GoogleError('Use at least one To recipient and at most15 total recipients.')
    if len(set(x.lower() for x in to+cc+bcc))!=len(to+cc+bcc):raise GoogleError('A recipient is in more than one field. Review To/CC/BCC.')
    if not isinstance(subject,str) or not isinstance(body,str) or not subject.strip() or len(subject)>200 or not body.strip() or len(body)>10000:raise GoogleError('Subject/body is empty or too long')
    if '\r' in subject or '\n' in subject:raise GoogleError('Subject must be one line.')
    from cr_safety import looks_like_secret,clean_text
    if looks_like_secret(json.dumps(text)):raise GoogleError('Draft appears to contain a secret')
    if not sender_name(uid):raise GoogleError('Set your email signature name first. No draft created.')
    body=re.sub(r'(?i)\[(?:your|sender(?:\'s)?)\s*(?:full\s*)?name\]|<your name>|\{your name\}',lambda m:sender_name(uid),body)
    signature=bool(isinstance(text,dict) and text.get('crayon_signature') is True)
    content={'from':row['email'],'to':to,'cc':cc,'bcc':bcc,'subject':clean_text(subject),'body':clean_text(body)}
    content['crayon_signature']=signature
    content['html']=__import__('cr_email_style').render(content['body'],signature)
    serialized=json.dumps(content,sort_keys=True)
    digest=hashlib.sha256(serialized.encode()).hexdigest()
    ident=secrets.token_hex(5)
    db.q("DELETE FROM google_email_drafts WHERE user_id=%s AND origin=%s AND status='pending'",(uid,channel),"none")
    db.q("INSERT INTO google_email_drafts(id,user_id,encrypted_content,content_hash,origin,expires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')",(ident,uid,encrypt(uid,content),digest,channel),"none")
    display=f"Draft only, not sent. Expires in 10 minutes.\nFrom: {content['from']}\nTo: {', '.join(to)}\nCC: {', '.join(cc) or 'none'}\nBCC: {', '.join(bcc) or 'none'}\nAttachments: none.\nFormat: clean HTML with plain-text alternative. Crayon AI signature: {'on' if signature else 'off'}.\nSubject: {content['subject']}\n\n{content['body']}"
    if structured:return {"text":display+"\n\nHTML uses a simple bordered white card, natural paragraph spacing, no remote images. Say add crayon signature / remove crayon signature to review that change.\n\nReply with edit/change/rewrite instructions to revise. Tap Send or say 'send it'. Say 'cancel' to discard.","id":ident,"hash":digest[:12],"html":content["html"],"fields":{k:content[k] for k in ("to","cc","bcc","subject","body","crayon_signature")}}
    return display+f"\n\nSend exactly this: /email_send {ident} {digest[:12]}\nCancel: /email_cancel {ident}"


def current_draft(uid):
    rows=db.q("SELECT id,content_hash FROM google_email_drafts WHERE user_id=%s AND origin='telegram' AND status='pending' AND expires_at>now()",(uid,),"all")
    if len(rows)!=1:raise GoogleError("No single current draft to confirm. Create or review a fresh email first.")
    return rows[0]["id"],rows[0]["content_hash"][:12]



def send_draft(uid, ident, short_hash, channel='telegram'):
    if db.kv_get('google_signature_pending_'+str(uid),None):raise GoogleError('Finish your signature name and review the new draft first.')
    import base64
    from email.message import EmailMessage
    # Atomic claim prevents replay and duplicate sends. Never retry an uncertain send.
    row=db.q("UPDATE google_email_drafts SET status='sending' WHERE id=%s AND user_id=%s AND origin=%s AND status='pending' AND expires_at>now() AND left(content_hash,12)=%s RETURNING encrypted_content,content_hash",(ident,uid,channel,short_hash),"one")
    if not row:raise GoogleError("Draft expired, already used, or confirmation did not match")
    try:
        content=decrypt(uid,row["encrypted_content"])
        if hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()!=row["content_hash"]:
            raise GoogleError("Draft content changed; create a new draft")
        if status(uid).get("email")!=content["from"]:raise GoogleError("Connected account changed; create a new draft")
        m=EmailMessage();m["From"]=content["from"];m["To"]=", ".join(content["to"]) if isinstance(content["to"],list) else content["to"];m["Subject"]=content["subject"];m.set_content(content["body"])
        if content.get('html'):m.add_alternative(content['html'],subtype='html')
        if content.get('cc'):m['Cc']=', '.join(content['cc'])
        if content.get('bcc'):m['Bcc']=', '.join(content['bcc'])
        token=_access(uid)
        with httpx.Client(timeout=20) as client:
            r=client.post("https://gmail.googleapis.com/gmail/v1/users/me/messages/send",headers={"Authorization":"Bearer "+token},json={"raw":base64.urlsafe_b64encode(m.as_bytes()).decode()})
        if r.status_code!=200 or not r.json().get("id"):raise GoogleError("Google did not confirm this send. Do not resend until you check Sent mail.")
        mid=r.json()["id"]
        db.q("DELETE FROM google_email_drafts WHERE id=%s AND user_id=%s",(ident,uid),"none")
        db.audit(uid,"google_email_sent",mid)
        db.audit(uid,"google_email_send_receipt",json.dumps({"message_id":mid,"channel":channel,"from":content["from"],"to":content["to"],"cc":content.get("cc",[]),"bcc":content.get("bcc",[]),"subject":content["subject"]}))
        return "Email sent to "+", ".join(content["to"])+". Google confirmed the send."
    except Exception:
        db.q("UPDATE google_email_drafts SET status='uncertain' WHERE id=%s AND user_id=%s",(ident,uid),"none")
        raise GoogleError("Send stopped or outcome is uncertain. Check Gmail Sent before creating another draft.") from None


def cancel_draft(uid, ident, channel='telegram'):
    db.q("DELETE FROM google_email_drafts WHERE id=%s AND user_id=%s AND origin=%s AND status='pending'",(ident,uid,channel),"none")
    return "Pending draft removed if it existed. No email sent."
