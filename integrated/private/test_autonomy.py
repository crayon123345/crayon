import pytest,time
import cr_calendar_draft as K
import cr_mail_watch as W
import cr_google as G

def content():return {'account':'me@example.com','summary':'Study','start':'2099-01-01T10:00:00+05:30','end':'2099-01-01T11:00:00+05:30','timezone':'Asia/Calcutta'}
def test_calendar_validate():
    assert K.validate(content())
    for change in ({'start':'2099-01-01T10:00:00'},{'end':'2099-01-01T09:00:00+05:30'},{'timezone':'America/New_York'},{'attendees':[]}):
        with pytest.raises(G.GoogleError):K.validate({**content(),**change})
def test_calendar_preview_no_network(monkeypatch):
    monkeypatch.setattr(K.db,'q',lambda *a,**kw:None)
    monkeypatch.setattr(K.db,'kv_set',lambda *a:None)
    monkeypatch.setattr(G,'status',lambda *a:{'email':'me@example.com'})
    monkeypatch.setattr(G,'encrypt',lambda u,c:'encrypted')
    monkeypatch.setattr(G,'request',lambda *a:pytest.fail('write while preview'))
    d=K.preview(K.OWNER,'Study',content()['start'],content()['end'],content()['timezone'])
    assert 'not booked' in d['text'] and 'Guests: none' in d['text']
def test_calendar_stale_user_rejected(monkeypatch):
    monkeypatch.setattr(K.db,'q',lambda *a,**kw:None)
    monkeypatch.setattr(K.db,'kv_get',lambda *a:None)
    with pytest.raises(G.GoogleError,match='Review'):K.create(K.OWNER,'id','hash')
def test_mail_metadata_only_dedup(monkeypatch):
    monkeypatch.setattr(G,'status',lambda *a:{'email':'me@example.com'})
    calls=[]
    def req(uid,url,params):
        calls.append((url,params))
        if url.endswith('/messages'):return {'messages':[{'id':'a'},{'id':'b'}]}
        return {'snippet':'Bounded source excerpt','labelIds':['IMPORTANT'] if url.endswith('a') else [],'payload':{'headers':[{'name':'From','value':'external@example.com'},{'name':'Subject','value':'Deadline' if url.endswith('a') else 'Newsletter'}]}}
    monkeypatch.setattr(G,'request',req)
    state={'email':'me@example.com','since':100,'seen':[]}
    text,state=W.scan(W.OWNER,state)
    assert 'POSSIBLE ATTENTION' in text and 'OTHER RECENT MAIL' in text and 'Bounded source excerpt' in text
    assert all(p.get('format')=='metadata' for u,p in calls if not u.endswith('/messages'))
    assert W.scan(W.OWNER,state)[0]==''
def test_mail_quiet_and_off(monkeypatch):
    monkeypatch.setattr(W.db,'kv_get',lambda *a:None)
    class Out:
        def send(self,*a):pytest.fail('off sent')
    W.tick_user(Out(),W.OWNER)
    monkeypatch.setattr(W.db,'kv_get',lambda *a:{'checked':0})
    monkeypatch.setattr(W.P,'settings',lambda *a:{'quiet_start':21,'quiet_end':9})
    monkeypatch.setattr(W.P.T,'now_local',lambda *a:type('N',(),{'hour':1})())
    monkeypatch.setattr(W,'scan',lambda *a:pytest.fail('quiet read'))
    W.tick_user(Out(),W.OWNER)
def test_mail_per_account_configure(monkeypatch):
    stored={}
    monkeypatch.setattr(W.G,'status',lambda uid:{'email':str(uid)+'@example.com'})
    monkeypatch.setattr(W.db,'kv_set',lambda k,v:stored.update({k:v}))
    W.configure(12,12,True);W.configure(13,13,True)
    assert stored[W.KEY(12)]['email']=='12@example.com' and stored[W.KEY(13)]['email']=='13@example.com'
    for uid,chat in ((12,13),(10**15,10**15)):
        with pytest.raises(G.GoogleError):W.configure(uid,chat,True)
def test_conflict_and_scope_no_write(monkeypatch):
    import json,hashlib
    c=content();digest=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(K.db,'kv_get',lambda *a:['id',digest[:12]])
    monkeypatch.setattr(K.db,'q',lambda sql,*a,**kw:{'encrypted_content':'x','content_hash':digest} if 'RETURNING encrypted_content' in sql else None)
    monkeypatch.setattr(G,'decrypt',lambda *a:c)
    monkeypatch.setattr(G,'status',lambda *a:{'email':c['account']})
    monkeypatch.setattr(G,'request',lambda *a:{'items':[{'summary':'Busy'}]})
    monkeypatch.setattr(G,'_access',lambda *a:pytest.fail('write conflict'))
    with pytest.raises(G.GoogleError,match='conflicts'):K.create(K.OWNER,'id',digest[:12])
def test_calendar_preview_route(monkeypatch):
    import cr_google_chat as H,cr_telegram as T
    monkeypatch.setattr(K,'preview',lambda *a,**kw:{'id':'id','hash':'hash','text':'Preview only'})
    class Out:
        def __init__(self):self.sent=[]
        def send(self,chat,text,markup=None):self.sent.append({'text':text,'markup':markup})
    out=Out();H.handle(K.OWNER,K.OWNER,'/calendar_slot Study | 2099-01-01T10:00:00+05:30 | 2099-01-01T11:00:00+05:30 | Asia/Calcutta',{},out)
    assert out.sent[0]['markup']['inline_keyboard'][0][0]['text']=='Create'
def test_supported_calendar_write_scope_upfront_review_still_required(monkeypatch):
    from urllib.parse import urlsplit,parse_qs
    monkeypatch.setattr(G,'configured',lambda:True)
    monkeypatch.setattr(G.db,'q',lambda *a,**kw:{'user_id':K.OWNER})
    monkeypatch.setattr(G.db,'kv_get',lambda *a:False)
    url=G.authorization_url('a'*43)
    scope=parse_qs(urlsplit(url).query)['scope'][0]
    assert 'calendar.events.readonly' not in scope and 'calendar.events' in scope
    monkeypatch.setattr(G.db,'kv_get',lambda *a:True)
    scope=parse_qs(urlsplit(G.authorization_url('a'*43)).query)['scope'][0]
    assert 'calendar.events.readonly' not in scope and 'calendar.events' in scope
def test_calendar_create_readback_exact(monkeypatch):
    import json,hashlib
    c=content();digest=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()
    queries=[]
    def q(sql,*a,**kw):
        queries.append(sql)
        return {'encrypted_content':'x','content_hash':digest} if 'RETURNING encrypted_content' in sql else None
    monkeypatch.setattr(K.db,'q',q)
    monkeypatch.setattr(K.db,'kv_get',lambda *a:['a1b2c3',digest[:12]])
    monkeypatch.setattr(G,'decrypt',lambda *a:c)
    monkeypatch.setattr(G,'status',lambda *a:{'email':c['account']})
    monkeypatch.setattr(G,'request',lambda *a:{'items':[]})
    monkeypatch.setattr(G,'_access',lambda *a:'fake')
    class Resp:
        status_code=200
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class Client:
        body=None;posts=0
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,url,**kw):
            if url.endswith('tokeninfo'):return Resp({'scope':'https://www.googleapis.com/auth/calendar.events'})
            return Resp({**Client.body,'htmlLink':'https://calendar.google.com/test-fixture'})
        def post(self,url,**kw):
            Client.posts+=1;Client.body=kw['json'];assert kw['params']=={'sendUpdates':'none'}
            assert 'attendees' not in Client.body and 'conferenceData' not in Client.body
            return Resp(Client.body)
    monkeypatch.setattr(G.httpx,'Client',Client)
    assert 'read back' in K.create(K.OWNER,'a1b2c3',digest[:12]);assert Client.posts==1
    assert any(x.startswith('DELETE FROM google_calendar_drafts WHERE id') for x in queries)

def test_mail_clean_entities(monkeypatch):
    monkeypatch.setattr(W.G,'status',lambda uid:{'email':'owner@example.com'})
    def request(uid,url,params):
        if url.endswith('/messages'):return {'messages':[{'id':'x'}]}
        return {'payload':{'headers':[{'name':'Subject','value':'Let&#39;s work'},{'name':'From','value':'Sam <sam@example.com>'}]},'snippet':'I didn&#39;t forget','labelIds':['IMPORTANT']}
    monkeypatch.setattr(W.G,'request',request)
    text,_=W.scan(W.OWNER,{'email':'owner@example.com','since':0,'seen':[]})
    assert "Let's work" in text and "didn't" in text and '&#' not in text and '<sam@' not in text

def test_mail_status_readonly(monkeypatch):
    monkeypatch.setattr(W.db,'kv_get',lambda *a:None)
    monkeypatch.setattr(G,'request',lambda *a:pytest.fail('status must not read mail'))
    assert 'off' in W.status(W.OWNER)
    monkeypatch.setattr(W.db,'kv_get',lambda *a:{'email':'test@example.com','checked':100,'paused':True})
    assert 'paused' in W.status(W.OWNER) and 'timestamp alone' in W.status(W.OWNER)
    assert "paused" in W.status(12)

def test_calendar_per_account_preview(monkeypatch):
    seen=[]
    monkeypatch.setattr(K.db,'q',lambda sql,args=(),*rest,**kw:seen.append(args))
    monkeypatch.setattr(K.db,'kv_set',lambda *a:None)
    monkeypatch.setattr(G,'status',lambda uid:{'email':str(uid)+'@example.com'})
    monkeypatch.setattr(G,'encrypt',lambda uid,c: 'encrypted_'+str(uid))
    for uid in (12,13):
        r=K.preview(uid,'test',content()['start'],content()['end'],'Asia/Calcutta')
        assert str(uid)+'@example.com' in r['text']
    assert any(12 in a for a in seen) and any(13 in a for a in seen)
    for uid in (0,-1,True):
        with pytest.raises(G.GoogleError):K.owner(uid)
def test_calendar_guests_and_reminder_validation():
    assert K.validate({**content(),'guests':['guest@example.com'],'reminder_minutes':30})
    for change in ({'guests':['invalid']},{'guests':['g@example.com','g@example.com']},{'guests':[[]]},{'reminder_minutes':-1},{'reminder_minutes':'30'}):
        with pytest.raises(G.GoogleError):K.validate({**content(),'guests':[],'reminder_minutes':None,**change})
def test_each_account_oauth_calendar_explicit_write(monkeypatch):
    from urllib.parse import urlsplit,parse_qs
    monkeypatch.setattr(G,'configured',lambda:True);monkeypatch.setattr(G.db,'q',lambda *a,**kw:{'user_id':7555366869});monkeypatch.setattr(G.db,'kv_get',lambda *a:True)
    assert 'calendar.events' in parse_qs(urlsplit(G.authorization_url('a'*43)).query)['scope'][0]

def test_guest_invites_and_reminder_exact_readback(monkeypatch):
    import json,hashlib
    c={**content(),'guests':['guest@example.com'],'reminder_minutes':20};digest=hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest();writes=[]
    monkeypatch.setattr(K.db,'q',lambda sql,*a,**kw:{'encrypted_content':'x','content_hash':digest} if 'RETURNING encrypted_content' in sql else None)
    monkeypatch.setattr(K.db,'kv_get',lambda *a:['a1b2c3',digest[:12]]);monkeypatch.setattr(G,'decrypt',lambda *a:c);monkeypatch.setattr(G,'status',lambda *a:{'email':c['account']});monkeypatch.setattr(G,'request',lambda *a:{'items':[]});monkeypatch.setattr(G,'_access',lambda *a:'fake')
    class Resp:
        status_code=200
        def __init__(self,data):self.data=data
        def json(self):return self.data
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def get(self,url,**kw):return Resp({'scope':'https://www.googleapis.com/auth/calendar.events'}) if url.endswith('tokeninfo') else Resp({**writes[0]['json'],'htmlLink':'https://calendar.google.com/test-fixture'})
        def post(self,url,**kw):writes.append(kw);return Resp(kw['json'])
    monkeypatch.setattr(G.httpx,'Client',Client)
    assert 'Email delivery is not independently confirmed' in K.create(K.OWNER,'a1b2c3',digest[:12])
    assert len(writes)==1 and writes[0]['params']=={'sendUpdates':'all'}
    assert writes[0]['json']['attendees']==[{'email':'me@example.com'},{'email':'guest@example.com'}]
    assert writes[0]['json']['reminders']=={'useDefault':False,'overrides':[{'method':'popup','minutes':20}]}


def test_watch_tick_routes_only_own_optins(monkeypatch):
    import contextlib
    rows=[{'key':'mail_watch_12'},{'key':'mail_watch_13'},{'key':'mail_watch_bad'}]
    states={'mail_watch_12':{'chat':12,'checked':0},'mail_watch_13':{'chat':13,'checked':0}}
    monkeypatch.setattr(W.db,'q',lambda *a,**kw:rows)
    monkeypatch.setattr(W.db,'kv_get',lambda key,*a:states.get(key))
    monkeypatch.setattr(W.db,'kv_set',lambda key,val:states.update({key:val}))
    monkeypatch.setattr(W.P,'settings',lambda uid:{'quiet_start':21,'quiet_end':9})
    monkeypatch.setattr(W.P.T,'now_local',lambda uid:type('N',(),{'hour':12})())
    monkeypatch.setattr(W.P.mem,'user_lock',lambda uid:contextlib.nullcontext())
    monkeypatch.setattr(W,'scan',lambda uid,state:('private_'+str(uid),state))
    sent=[]
    class Out:
        def send(self,uid,text):sent.append((uid,text))
    W.tick(Out());assert sent==[(12,'private_12'),(13,'private_13')]
    states['mail_watch_12']={'chat':13,'checked':0};sent.clear();W.tick(Out());assert not sent
