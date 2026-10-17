"""Discord transport for the canonical private engine, not the public chat-only bot.
No token => dormant. No verified pilot allowlist => refuse startup. No arbitrary
Discord ID is ever used as a Telegram/canonical owner ID. All sends disable pings.
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import secrets
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

import httpx
import cr_db as db

log = logging.getLogger('crayon.discord')
API = 'https://discord.com/api/v10'
MAX_BYTES = 20_000_000
SCHEMA = '''
CREATE TABLE IF NOT EXISTS discord_accounts(
 subject TEXT PRIMARY KEY, user_id BIGINT UNIQUE NOT NULL,
 dm_channel TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS discord_inbox(
 id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, state TEXT NOT NULL,
 created_at TIMESTAMPTZ DEFAULT now());
CREATE TABLE IF NOT EXISTS discord_controls(
 id TEXT PRIMARY KEY, user_id BIGINT NOT NULL, channel_id TEXT NOT NULL,
 message_id TEXT NOT NULL DEFAULT '', encrypted TEXT NOT NULL,
 expires_at TIMESTAMPTZ NOT NULL, state TEXT NOT NULL DEFAULT 'prepared');
'''
_rate = defaultdict(deque)
_rate_lock = threading.Lock()


def snowflake(value):
    text = str(value)
    if not re.fullmatch(r'[1-9][0-9]{16,19}', text) or int(text) >= 2**63:
        raise ValueError('Invalid Discord identifier')
    return text


def allowed_users():
    return {snowflake(x.strip()) for x in os.getenv('CRAYON_DISCORD_ALLOWED_USERS', '').split(',') if x.strip()}


def allowed_guilds():
    return {snowflake(x.strip()) for x in os.getenv('CRAYON_DISCORD_ALLOWED_GUILDS', '').split(',') if x.strip()}


def configured():
    return bool(os.getenv('CRAYON_DISCORD_ENABLED')=='on' and os.getenv('DISCORD_BOT_TOKEN') and allowed_users() and db.available())


def init():
    if not db.available():
        raise RuntimeError('Discord requires canonical persistent storage')
    __import__('cr_accounts').init()
    for stmt in SCHEMA.split(';'):
        if stmt.strip():
            db.q(stmt, fetch='none')
    # Never replay a request after a crash: it may already have changed something.
    db.q("UPDATE discord_inbox SET state='uncertain' WHERE state='processing'", fetch='none')
    db.q("UPDATE discord_controls SET state='uncertain',encrypted='' WHERE state='claimed'", fetch='none')
    db.q("DELETE FROM discord_controls WHERE expires_at<=now()", fetch='none')
    db.q("DELETE FROM discord_inbox WHERE created_at<now()-interval '7 days'", fetch='none')


def cleanup(uid):
    """Included in canonical account erasure, even when the transport is disabled."""
    for stmt in SCHEMA.split(';'):
        if stmt.strip():db.q(stmt,fetch='none')
    for table in ('discord_controls','discord_inbox','discord_accounts'):
        db.q('DELETE FROM '+table+' WHERE user_id=%s',(uid,),'none')


def account(subject, dm_channel, name=''):
    """Only called after a Discord Gateway event from the authenticated bot session."""
    subject, dm_channel = snowflake(subject), snowflake(dm_channel)
    if subject not in allowed_users():
        raise ValueError('Discord user is not enabled for this pilot')
    with db._conn().transaction():
        db.q('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))', ('crayon-discord:'+subject,), 'none')
        row = db.q('SELECT d.user_id FROM discord_accounts d JOIN users u ON u.user_id=d.user_id WHERE d.subject=%s', (subject,), 'one')
        if row:
            db.q('UPDATE discord_accounts SET dm_channel=%s WHERE subject=%s', (dm_channel,subject), 'none')
            return row['user_id']
        # Deleted accounts may leave routing tombstones; never resurrect old private state.
        db.q('DELETE FROM discord_accounts WHERE subject=%s', (subject,), 'none')
        uid = db.q("SELECT nextval('crayon_account_ids') AS id", fetch='one')['id']
        db.q('INSERT INTO users(user_id,name) VALUES(%s,%s)', (uid,str(name)[:100]), 'none')
        db.q('INSERT INTO discord_accounts(subject,user_id,dm_channel) VALUES(%s,%s,%s)', (subject,uid,dm_channel), 'none')
        return uid


def destination(uid):
    if os.getenv('CRAYON_DISCORD_ENABLED')!='on' or not os.getenv('DISCORD_BOT_TOKEN') or not db.available():
        return None
    row = db.q('SELECT d.subject,d.dm_channel FROM discord_accounts d JOIN users u ON u.user_id=d.user_id WHERE d.user_id=%s', (uid,), 'one')
    return row if row and row['subject'] in allowed_users() else None


def limited(subject):
    with _rate_lock:
        now = time.monotonic()
        q = _rate[subject]
        while q and q[0] <= now-60:
            q.popleft()
        if len(q) >= 8:
            return True
        q.append(now)
        return False


def claim(ident, uid):
    return bool(db.q("INSERT INTO discord_inbox(id,user_id,state) VALUES(%s,%s,'processing') ON CONFLICT DO NOTHING RETURNING id", (str(ident),uid),'one'))


def finish(ident, state):
    if state not in ('done','uncertain'):
        raise ValueError('Invalid completion state')
    db.q('UPDATE discord_inbox SET state=%s WHERE id=%s', (state,str(ident)), 'none')


def chunks(text, limit=1900):
    """Respect Discord's UTF-16 text limit, including non-BMP emoji."""
    part=[]; size=0
    for c in str(text) or '(empty)':
        width=2 if ord(c)>0xffff else 1
        if size+width>limit:
            yield ''.join(part); part=[]; size=0
        part.append(c);size+=width
    if part:
        yield ''.join(part)


class Transport:
    def __init__(self, client=None):
        self.client=client or httpx.Client(timeout=httpx.Timeout(35,connect=10),follow_redirects=False)

    def request(self, method, path, **kwargs):
        # A single attempt. A timeout/429/5xx is uncertain, never an automatic replay.
        r=self.client.request(method, API+path,
            headers={'Authorization':'Bot '+os.environ['DISCORD_BOT_TOKEN']}, **kwargs)
        if not 200<=r.status_code<300:
            raise RuntimeError('Discord operation not confirmed (HTTP '+str(r.status_code)+')')
        if r.status_code==204:
            return {}
        result=r.json()
        if not isinstance(result,dict):
            raise RuntimeError('Invalid Discord response')
        return result

    def message(self, channel, text='', components=None, item=None):
        body={'content':text,'allowed_mentions':{'parse':[],'replied_user':False},
              'nonce':secrets.token_hex(12),'enforce_nonce':True}
        if components:
            body['components']=components
        kwargs={'json':body}
        if item:
            body['attachments']=[{'id':0,'filename':item['filename']}]
            kwargs={'data':{'payload_json':json.dumps(body)},
                    'files':{'files[0]':(item['filename'],item['data'],item['mime'])}}
        r=self.request('POST','/channels/'+snowflake(channel)+'/messages',**kwargs)
        return snowflake(r['id'])


def encrypt(value):
    return __import__('cr_web_auth')._cipher().encrypt(value.encode()).decode()


def decrypt(value):
    return __import__('cr_web_auth')._cipher().decrypt(value.encode()).decode()


class Out:
    """Canonical send interface. Request controls bind owner, channel AND message."""
    def __init__(self, uid, channel, transport=None, subject=None, source_message=None):
        self.uid=uid;self.channel=snowflake(channel);self.transport=transport or Transport()
        self.subject=subject;self.source_message=source_message;self.meta={}

    def send(self, chat_id, text, markup=None):
        if chat_id!=self.uid:
            raise ValueError('Discord private output cannot change audience')
        from cr_safety import clean_text
        text=clean_text(str(text)) or '(empty)'
        # Shared engine's Telegram wording is not a claim of Telegram delivery.
        text=text.replace('your Telegram account','your Crayon Discord account')
        text=text.replace('Telegram delivery','Discord delivery')
        ids=[self.transport.message(self.channel,p) for p in chunks(text)]
        buttons=[]
        if isinstance(markup,dict):
            for row in markup.get('inline_keyboard',[]):
                for b in row:
                    if b.get('callback_data'):
                        buttons.append((str(b['text']),str(b['callback_data'])))
            # Convert help reply-keyboard shortcuts into requester-bound actions too.
            for row in markup.get('keyboard',[]):
                for b in row:
                    title=b.get('text') if isinstance(b,dict) else b
                    if title:
                        buttons.append((str(title),'command:'+str(title)))
        if len(buttons)>25:
            raise ValueError('Too many review controls; no action was approved')
        if buttons:
            rows=[];tokens=[]
            try:
                for i,(label,data) in enumerate(buttons):
                    ident=secrets.token_urlsafe(24);tokens.append(ident)
                    db.q("INSERT INTO discord_controls(id,user_id,channel_id,encrypted,expires_at) VALUES(%s,%s,%s,%s,now()+interval '10 minutes')",(ident,self.uid,self.channel,encrypt(data)),'none')
                    if i%5==0:rows.append({'type':1,'components':[]})
                    rows[-1]['components'].append({'type':2,'style':2,'label':label[:80],'custom_id':'crayon:'+ident})
                mid=self.transport.message(self.channel,'Review the complete text above before choosing.',components=rows)
                db.q("UPDATE discord_controls SET message_id=%s,state='ready' WHERE id=ANY(%s) AND state='prepared'",(mid,tokens),'none')
                ids.append(mid)
            except Exception:
                # Partial review delivery is not authority to act.
                for ident in tokens:
                    db.q("UPDATE discord_controls SET state='uncertain',encrypted='' WHERE id=%s",(ident,),'none')
                raise
        return ids

    def artifact(self, chat_id, item):
        if chat_id!=self.uid or len(item['data'])>2_000_000:
            raise ValueError('Artifact exceeds the canonical 2 MB limit or changes audience')
        name=str(item['filename'])
        if not name or len(name)>150 or any(c in name for c in ('/','\\','\r','\n')):
            raise ValueError('Unsafe artifact filename')
        return self.transport.message(self.channel,item=item)

    def typing(self, chat_id):
        if chat_id==self.uid:
            try:self.transport.request('POST','/channels/'+self.channel+'/typing')
            except Exception:pass

    def react(self,chat_id,message_id,emoji):
        # Optional reactions are deliberately not reported as successful without readback.
        return False

    def delete(self,chat_id,message_id):
        # A bot cannot delete other people's DM messages. Never claim it did.
        return False

    def acknowledge_callback(self, ident):
        return None  # Gateway handler already acknowledged on Discord, never Telegram.


def callback(uid,channel,message_id,custom_id,out):
    if not isinstance(custom_id,str) or not re.fullmatch(r'crayon:[A-Za-z0-9_-]{32}',custom_id):
        return False
    ident=custom_id.split(':',1)[1]
    row=db.q("UPDATE discord_controls SET state='claimed' WHERE id=%s AND user_id=%s AND channel_id=%s AND message_id=%s AND state='ready' AND expires_at>now() RETURNING encrypted",(ident,uid,snowflake(channel),snowflake(message_id)),'one')
    if not row:
        return False
    try:
        data=decrypt(row['encrypted'])
        if data.startswith('command:'):
            dispatch_text(uid,'',data[8:],None,out)
        else:
            __import__('cr_telegram').handle_callback({'id':ident,'from':{'id':uid},'data':data,'message':{'chat':{'id':uid}}},out)
        db.q("UPDATE discord_controls SET state='used',encrypted='' WHERE id=%s",(ident,),'none')
    except Exception:
        db.q("UPDATE discord_controls SET state='uncertain',encrypted='' WHERE id=%s",(ident,),'none')
        raise
    return True


def dispatch_text(uid,name,text,message_id,out,quote=None):
    import cr_telegram as T,cr_channel as CH,cr_memory as M,cr_reply_context as R
    if not isinstance(text,str) or not 1<=len(text)<=4000:
        out.send(uid,'Send between 1 and 4,000 characters.');return
    # No fake Telegram update/ID and no tg_latest mutation. Only common engine path.
    if __import__('cr_safety').looks_like_secret(text):
        out.send(uid,'That looks like a secret. It was not processed or saved. Bots cannot delete your Discord DM message. Delete it and rotate the secret.');return
    if text.strip().lower() in ('forget me','delete my data'):
        text='/delete_my_data'
    if text.strip().lower() in ('/rooms','shared rooms'):
        out.send(uid,'Discord server channels are shared audiences. This pilot keeps personal tools in DMs. Web Shared rooms remain available in the web app; histories are not silently bridged.');return
    if text.strip().lower() in ('/help','help'):
        out.send(uid,T.HELP.split('In a group,')[0]+'\nDiscord pilot: personal commands and reviewed actions run here in your DM. Existing Telegram/web data is not imported. In a server, mention me for public-only questions. Voice-channel calls and account merging are not enabled.');return
    channel_token=CH.channel.set('discord')
    quote_token=R.current.set(quote)
    try:
        with M.user_lock(uid):
            progress=T.ProgressOut(out,uid,((12,'Working on it.'),(35,'Still checking.'))).start()
            try:
                __import__('cr_history').run(uid,uid,name,text,message_id,progress,T._handle_text)
            finally:progress.stop()
    finally:
        R.current.reset(quote_token);CH.channel.reset(channel_token)


def media(uid,name,attachment,caption,out):
    """Discord-authenticated attachment metadata only; strict CDN, byte and redirect caps."""
    from cr_safety import looks_like_secret,redact,clean_text
    import cr_media as M,cr_memory as mem,cr_telegram as T
    if looks_like_secret(caption):
        out.send(uid,'The caption looks like a secret. It was not processed or saved. Discord does not let bots delete your DM message. Delete it and rotate the secret.');return
    url=attachment['url'];u=urlsplit(url)
    if u.scheme!='https' or u.hostname not in ('cdn.discordapp.com','media.discordapp.net') or u.username or u.password or u.port not in (None,443) or not u.path.startswith('/attachments/'):
        raise ValueError('Attachment URL is not a Discord CDN upload')
    if type(attachment['size']) is not int or not 0<attachment['size']<=MAX_BYTES:
        raise ValueError('Uploads must be at most 20 MB')
    with mem.user_lock(uid):
        mem.touch_user(uid,name)
        if T._over_cap(uid):
            out.send(uid,"Today's free-tier message limit is reached.");return
        out.send(uid,'Reading your uploaded file. Raw files are not kept in Crayon history.')
        data=bytearray()
        with httpx.stream('GET',url,timeout=45,follow_redirects=False) as r:
            if r.status_code!=200:raise ValueError('Attachment download not confirmed')
            for part in r.iter_bytes():
                data.extend(part)
                if len(data)>MAX_BYTES:raise ValueError('Upload exceeds 20 MB')
        if len(data)!=attachment['size']:raise ValueError('Attachment size changed')
        answer=M.analyze(bytes(data),attachment.get('mime','application/octet-stream'),caption,attachment['filename'])
        mem.add_message(uid,'user','[User sent media: '+attachment.get('mime','')+'] '+redact(caption))
        mem.add_message(uid,'assistant','[Media analysis summary; raw file not retained] '+clean_text(answer)[:3000])
        out.send(uid,answer or 'I could not read that upload clearly.')


def quote_context(message):
    reference=getattr(message,'reference',None)
    quoted=getattr(reference,'resolved',None)
    if not quoted or getattr(getattr(quoted,'channel',None),'id',None)!=message.channel.id:
        return None
    from cr_safety import looks_like_secret,redact
    text=getattr(quoted,'content','')
    if not text:return None
    return {'text':'[Secret withheld]' if looks_like_secret(text) else redact(text)[:6000],
            'source':'Discord reply '+str(quoted.id),'role':'bot' if quoted.author.bot else 'participant'}


def build_client():
    """Build the real Gateway client without opening a network connection."""
    import discord
    from discord import app_commands
    intents=discord.Intents.default();intents.message_content=True
    client=discord.Client(intents=intents,allowed_mentions=discord.AllowedMentions.none(),max_messages=100)
    gate=asyncio.Semaphore(4);pending=set()
    tree=app_commands.CommandTree(client)
    client.crayon_tree=tree

    async def setup_hook():
        await tree.sync()
    client.setup_hook=setup_hook

    @tree.command(name='crayon',description='Ask Crayon in your private DM, with an optional file')
    @app_commands.describe(text='Your request (up to 4,000 characters)',attachment='Optional one file, up to 20 MB')
    async def crayon_command(interaction: discord.Interaction, text: str, attachment: discord.Attachment = None):
        subject=str(interaction.user.id)
        if subject not in allowed_users():
            await interaction.response.send_message('This account is not enabled for the free pilot.',ephemeral=True);return
        if len(text)>4000 or limited(subject) or len(pending)>=16:
            await interaction.response.send_message('Request not accepted. Use at most 4,000 characters and wait for current work to finish.',ephemeral=True);return
        await interaction.response.defer(ephemeral=True,thinking=True)
        ident=str(interaction.id);pending.add(ident)
        try:
            dm=await interaction.user.create_dm()
            async with gate:
                uid=await asyncio.to_thread(account,subject,str(dm.id),interaction.user.display_name)
                if not await asyncio.to_thread(claim,ident,uid):
                    await interaction.followup.send('This request was already accepted. It was not repeated.',ephemeral=True);return
                out=Out(uid,str(dm.id),subject=subject)
                try:
                    if attachment:
                        await asyncio.to_thread(media,uid,interaction.user.display_name,{'url':attachment.url,'filename':attachment.filename,'size':attachment.size,'mime':attachment.content_type or 'application/octet-stream'},text,out)
                    else:
                        await asyncio.to_thread(dispatch_text,uid,interaction.user.display_name,text,ident,out)
                    await asyncio.to_thread(finish,ident,'done')
                except Exception:
                    await asyncio.to_thread(finish,ident,'uncertain');raise
            await interaction.followup.send('Crayon replied in your private DM.',ephemeral=True)
        except Exception:
            await interaction.followup.send('Reply not confirmed. Check whether DMs are allowed. Do not repeat an external action before checking its status.',ephemeral=True)
        finally:pending.discard(ident)

    @client.event
    async def on_ready():
        log.info('Discord Gateway ready; private pilot allowlist enforced')

    @client.event
    async def on_message(msg):
        subject=str(msg.author.id)
        if msg.author.bot or msg.webhook_id or subject not in allowed_users():return
        if msg.guild:
            if str(msg.guild.id) not in allowed_guilds() or not client.user.mentioned_in(msg):return
            text=re.sub(r'<@!?'+str(client.user.id)+r'>','',msg.content).strip()
            if not text or len(text)>4000 or limited(subject) or len(pending)>=16:return
            # Public-only path does not allocate/read a personal account.
            if __import__('cr_group_actions').action_request(text):
                await msg.reply('Personal information and actions stay in your private Crayon DM in this Discord pilot. Nothing private was read here.',mention_author=False);return
            try:
                async with gate:
                    reply=await asyncio.to_thread(__import__('cr_group').answer,{'text':text})
                for p in chunks(reply):await msg.reply(p,mention_author=False)
            except Exception:await msg.reply('Public answer not confirmed. No private account was accessed.',mention_author=False)
            return
        if getattr(msg.channel,'type',None)!=discord.ChannelType.private:return
        if limited(subject):
            await msg.reply('Please wait a minute. The free pilot allows eight requests per minute.',mention_author=False);return
        if len(pending)>=16:
            await msg.reply('The free pilot is busy. Your request was not accepted. Please wait before trying again.',mention_author=False);return
        pending.add(str(msg.id))
        try:
            async with gate:
                uid=await asyncio.to_thread(account,subject,str(msg.channel.id),msg.author.display_name)
                if not await asyncio.to_thread(claim,str(msg.id),uid):return
                out=Out(uid,str(msg.channel.id),subject=subject,source_message=str(msg.id))
                try:
                    if msg.attachments:
                        if len(msg.attachments)!=1:
                            await asyncio.to_thread(out.send,uid,'Send one file at a time. Nothing was processed.')
                            await asyncio.to_thread(finish,str(msg.id),'done')
                            return
                        a=msg.attachments[0]
                        await asyncio.to_thread(media,uid,msg.author.display_name,{'url':a.url,'filename':a.filename,'size':a.size,'mime':a.content_type or 'application/octet-stream'},msg.content,out)
                    else:
                        await asyncio.to_thread(dispatch_text,uid,msg.author.display_name,msg.content,str(msg.id),out,quote_context(msg))
                    await asyncio.to_thread(finish,str(msg.id),'done')
                except Exception:
                    await asyncio.to_thread(finish,str(msg.id),'uncertain')
                    log.warning('Discord request uncertain; no automatic replay')
                    try:await asyncio.to_thread(out.send,uid,'I could not confirm that request. Do not repeat an external action before checking its original status.')
                    except Exception:pass
        finally:pending.discard(str(msg.id))

    @client.event
    async def on_interaction(interaction):
        if interaction.type!=discord.InteractionType.component:return
        if str(interaction.user.id) not in allowed_users() or interaction.guild is not None or getattr(interaction.channel,'type',None)!=discord.ChannelType.private:
            await interaction.response.send_message('This control is not available to this account/audience.',ephemeral=True);return
        await interaction.response.defer(ephemeral=True,thinking=True)
        try:
            async with gate:
                uid=await asyncio.to_thread(account,str(interaction.user.id),str(interaction.channel_id),interaction.user.display_name)
                out=Out(uid,str(interaction.channel_id))
                ok=await asyncio.to_thread(callback,uid,str(interaction.channel_id),str(interaction.message.id),(interaction.data or {}).get('custom_id'),out)
            await interaction.followup.send('Review handled. Check the response in this DM.' if ok else 'This control expired, was already used, or belongs to another account. Nothing was approved.',ephemeral=True)
        except Exception:
            await interaction.followup.send('The outcome is uncertain. Check the original action before retrying. No automatic retry was made.',ephemeral=True)
    return client


def start():
    """One dedicated consumer only; no automatic service activation."""
    if not configured():
        raise RuntimeError('Set Discord token, verified test allowlist and canonical database before startup')
    client=build_client()
    client.run(os.environ['DISCORD_BOT_TOKEN'],log_handler=None)
