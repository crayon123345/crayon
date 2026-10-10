"""Private primary-calendar draft with exact review, atomic one-use create and readback."""
import re,json,time,secrets,hashlib
from datetime import datetime,timezone
import cr_google as G
import cr_db as db
OWNER=1898030949
def owner(uid):
    if type(uid) is not int or uid<=0:raise G.GoogleError('A valid authenticated account is required.')
URL='https://www.googleapis.com/calendar/v3/calendars/primary/events'
def init():
    db.q("CREATE TABLE IF NOT EXISTS google_calendar_drafts(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,encrypted_content TEXT NOT NULL,content_hash TEXT NOT NULL,status TEXT DEFAULT 'pending',expires_at TIMESTAMPTZ NOT NULL)",fetch='none')
    db.q("ALTER TABLE google_calendar_drafts ADD COLUMN IF NOT EXISTS origin TEXT DEFAULT 'telegram'",fetch='none')
def validate(content):
    if not isinstance(content,dict):raise G.GoogleError('Invalid calendar preview')
    if set(content)!={'account','summary','start','end','timezone'} and set(content)!={'account','summary','start','end','timezone','guests','reminder_minutes'}:raise G.GoogleError('Unexpected calendar fields')
    guests=content.get('guests',[]);minutes=content.get('reminder_minutes',None)
    if not isinstance(guests,list) or len(guests)>10 or any(not isinstance(e,str) or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',e) for e in guests) or len(set(guests))!=len(guests):raise G.GoogleError('Use up to 10 distinct guest email addresses')
    if minutes is not None and (type(minutes)!=int or not 0<=minutes<=40320):raise G.GoogleError('Reminder minutes must be 0-40320 or none')
    if not isinstance(content['summary'],str) or not content['summary'].strip() or len(content['summary'])>100:raise G.GoogleError('Use a title of 1-100 characters')
    from zoneinfo import ZoneInfo
    try:
        z=ZoneInfo(content['timezone']);a=datetime.fromisoformat(content['start']);b=datetime.fromisoformat(content['end'])
        if a.tzinfo is None or b.tzinfo is None or a.utcoffset()!=a.astimezone(z).utcoffset() or b.utcoffset()!=b.astimezone(z).utcoffset():raise ValueError()
        if b<=a or (b-a).total_seconds()>86400 or a<=datetime.now(timezone.utc):raise ValueError()
    except Exception:raise G.GoogleError('Use future ISO start/end with offsets matching the timezone; duration under 24 hours') from None
    return content
def preview(uid,title,start,end,tz,guests=None,reminder_minutes=None,channel='telegram'):
    owner(uid)
    init();db.q("DELETE FROM google_calendar_drafts WHERE expires_at<now()",fetch='none');account=G.status(uid).get('email')
    if not account:raise G.GoogleError('Connect Google first')
    from cr_safety import clean_text,looks_like_secret
    if looks_like_secret(title):raise G.GoogleError('Title appears to contain a secret')
    c=validate({'account':account,'summary':clean_text(title),'start':start,'end':end,'timezone':tz,'guests':guests or [],'reminder_minutes':reminder_minutes})
    h=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest();ident=secrets.token_hex(8)
    db.q("DELETE FROM google_calendar_drafts WHERE user_id=%s AND origin=%s AND status='pending'",(uid,channel),'none')
    db.q("INSERT INTO google_calendar_drafts(id,user_id,encrypted_content,content_hash,origin,expires_at) VALUES(%s,%s,%s,%s,%s,now()+interval '10 minutes')",(ident,uid,G.encrypt(uid,c),h,channel),'none')
    db.kv_set('calendar_reviewed_'+str(uid)+('_web' if channel=='web' else ''),[ident,h[:12]])
    human_start=datetime.fromisoformat(start).strftime('%A, %d %B %Y, %I:%M %p %z')
    human_end=datetime.fromisoformat(end).strftime('%A, %d %B %Y, %I:%M %p %z')
    return {'id':ident,'hash':h[:12],'text':'Calendar preview only, not booked. Expires in 10 minutes.\nAccount: '+account+'\nCalendar: primary\nTitle: '+c['summary']+'\nWhen: '+human_start+' to '+human_end+'\nExact start: '+start+'\nExact end: '+end+'\nTimezone: '+tz+'\nGuests: '+(', '.join(c['guests']) or 'none')+'\nNotifications: '+('email invitations will be sent to listed guests' if c['guests'] else 'none')+'\nReminder: '+('Google default' if reminder_minutes is None else str(reminder_minutes)+' minutes before, popup')+'\nPrivate primary-calendar event. No video link. Existing events checked again before Create. Creating is not a venue/service booking.'}
def cancel(uid,ident,channel='telegram'):
    owner(uid)
    init();db.q("DELETE FROM google_calendar_drafts WHERE id=%s AND user_id=%s AND origin=%s AND status='pending'",(ident,uid,channel),'none');return 'Preview cancelled. No calendar event created.'
def create(uid,ident,h,channel='telegram'):
    owner(uid)
    init()
    if db.kv_get('calendar_reviewed_'+str(uid)+('_web' if channel=='web' else ''),None)!=[ident,h]:raise G.GoogleError('Review the current preview first')
    row=db.q("UPDATE google_calendar_drafts SET status='creating' WHERE id=%s AND user_id=%s AND origin=%s AND status='pending' AND expires_at>now() AND left(content_hash,12)=%s RETURNING encrypted_content,content_hash",(ident,uid,channel,h),'one')
    if not row:raise G.GoogleError('Preview expired, changed or already used')
    try:
        c=G.decrypt(uid,row['encrypted_content']);validate(c)
        if hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()!=row['content_hash'] or G.status(uid).get('email')!=c['account']:raise G.GoogleError('Preview/account changed')
        events=G.request(uid,URL,{'timeMin':c['start'],'timeMax':c['end'],'singleEvents':'true','maxResults':100})
        if events.get('nextPageToken') or any(e.get('status')!='cancelled' and e.get('transparency')!='transparent' for e in events.get('items',[])):raise G.GoogleError('Slot conflicts or availability incomplete. Review a different slot.')
        token=G._access(uid)
        # Required scope is checked before write; existing read-only connections fail here.
        with G.httpx.Client(timeout=20) as client:
            info=client.get('https://oauth2.googleapis.com/tokeninfo',params={'access_token':token})
            if info.status_code!=200 or 'https://www.googleapis.com/auth/calendar.events' not in info.json().get('scope','').split():raise G.GoogleError('Reconnect Google with calendar write permission, then review a fresh preview')
            body={'id':'c0a'+ident,'summary':c['summary'],'start':{'dateTime':c['start'],'timeZone':c['timezone']},'end':{'dateTime':c['end'],'timeZone':c['timezone']},'visibility':'private'}
            guests=c.get('guests',[])
            if guests:body['attendees']=[{'email':c['account']}]+[{'email':e} for e in guests if e!=c['account']]
            if c.get('reminder_minutes') is not None:body['reminders']={'useDefault':False,'overrides':[{'method':'popup','minutes':c['reminder_minutes']}]}
            r=client.post(URL,params={'sendUpdates':'all' if guests else 'none'},json=body,headers={'Authorization':'Bearer '+token})
            if r.status_code not in (200,201):raise G.GoogleError('Calendar create unconfirmed. Check Calendar before retrying.')
            data=r.json()
            stored=client.get(URL+'/'+body['id'],headers={'Authorization':'Bearer '+token})
            if stored.status_code!=200:raise G.GoogleError('Created response received but readback failed. Check Calendar, do not retry.')
            e=stored.json()
            if e.get('visibility')!='private' or e.get('conferenceData') or e.get('id')!=body['id'] or e.get('summary')!=c['summary'] or {a.get('email') for a in e.get('attendees',[])}!={a.get('email') for a in body.get('attendees',[])} or (body.get('reminders') and e.get('reminders')!=body['reminders']) or any(datetime.fromisoformat(e[k]['dateTime'])!=datetime.fromisoformat(c[k]) for k in ('start','end')):raise G.GoogleError('Calendar readback mismatch. Check Calendar, do not retry.')
            link=e.get('htmlLink','')
        db.q('DELETE FROM google_calendar_drafts WHERE id=%s AND user_id=%s',(ident,uid),'none')
        return 'Created and read back on your primary calendar. '+('Guest invitations requested for: '+', '.join(guests)+'. Email delivery is not independently confirmed. ' if guests else 'No invitations. ')+link
    except Exception as e:
        db.q("UPDATE google_calendar_drafts SET status='stopped' WHERE id=%s AND user_id=%s",(ident,uid),'none')
        raise G.GoogleError(str(e) if isinstance(e,G.GoogleError) else 'Calendar outcome uncertain. Check Calendar, do not retry.') from None
