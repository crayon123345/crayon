import cr_group as G
import cr_telegram as T


def test_mention_exact():
    assert G.mentioned({'text':'hi @crayon_v1_bot explain gravity'})
    assert not G.mentioned({'text':'hi @crayon_v1_bot_fake'})
    assert not G.mentioned({'text':'hello'})


def test_untagged_group_ignored(monkeypatch):
    monkeypatch.setattr(T.db,'kv_set',lambda *a: (_ for _ in ()).throw(AssertionError('private state touched')))
    out=T.CaptureOut()
    T.handle_update({'message':{'chat':{'id':-991,'type':'group'},'from':{'id':22},'text':'hello'}},out)
    assert not out.sent


def test_tagged_group_no_private_state(monkeypatch):
    monkeypatch.setattr(T.db,'kv_set',lambda *a: (_ for _ in ()).throw(AssertionError('private state touched')))
    monkeypatch.setattr(G,'answer',lambda msg:'Group answer')
    out=T.CaptureOut()
    T.handle_update({'message':{'chat':{'id':-991,'type':'supergroup'},'from':{'id':22},'text':'@crayon_v1_bot hi'}},out)
    assert out.sent[0]['text']=='Group answer'


def test_group_tools_disabled(monkeypatch):
    def gen(contents,system,tools):
        assert tools is None
        assert 'no private memory' in system
        return {'text':'Hi'}
    monkeypatch.setattr(G.llm,'generate',gen)
    assert G.answer({'text':'@crayon_v1_bot explain gravity'})=='Hi'

def test_group_news_explicit_public_lookup(monkeypatch):
    import cr_web as W
    calls=[]
    monkeypatch.setattr(W,'news',lambda q,n,day:calls.append((q,day)) or {'items':[{'title':'Test news','url':'https://news.google.com/test','source':'Publisher','published':'2026-10-09T10:00:00+05:30'}],'day':None})
    monkeypatch.setattr(G.llm,'generate',lambda *a,**kw:(_ for _ in ()).throw(AssertionError('no unsupported model news')))
    r=G.answer({'text':'@crayon_v1_bot tell me the latest AI news now','from':{'id':7555366869}})
    assert calls and 'Test news' in r and 'https://' not in r and 'Publisher' in r

def test_group_yesterday_resolves_date(monkeypatch):
    import cr_web as W
    from datetime import datetime,timedelta
    from zoneinfo import ZoneInfo
    calls=[]
    monkeypatch.setattr(W,'news',lambda q,n,day:calls.append(day) or {'items':[]})
    G.answer({'text':'@crayon_v1_bot yesterday news of india','from':{'id':1898030949}})
    assert calls==[datetime.now(ZoneInfo('Asia/Calcutta')).date()-timedelta(days=1)]

def test_group_private_and_unknown_lookup_blocked(monkeypatch):
    import cr_web as W
    monkeypatch.setattr(W,'news',lambda *a:(_ for _ in ()).throw(AssertionError('blocked')))
    assert 'never read' in G.answer({'text':'@crayon_v1_bot search my inbox','from':{'id':1898030949}})

def test_group_lookup_failure_no_fluff(monkeypatch):
    import cr_web as W
    monkeypatch.setattr(W,'news',lambda *a:(_ for _ in ()).throw(RuntimeError('HTTP503')))
    r=G.answer({'text':'@crayon_v1_bot latest news','from':{'id':1898030949}})
    assert 'failed' in r and 'happy' not in r and 'cutoff' not in r

def test_news_date_filter(monkeypatch):
    import cr_web as W
    from datetime import date
    import httpx
    xml=b'<rss><channel><item><title>old</title><pubDate>Wed, 07 Oct 2026 10:00:00 GMT</pubDate><link>https://news.google.com/old</link></item><item><title>fresh</title><source>Publisher</source><pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate><link>https://news.google.com/fresh</link></item><item><title>unsafe</title><pubDate>Thu, 08 Oct 2026 11:00:00 GMT</pubDate><link>http://localhost/private</link></item></channel></rss>'
    monkeypatch.setattr(W._c,'get',lambda *a,**kw:httpx.Response(200,content=xml))
    r=W.news('fresh',3,date(2026,10,8));assert [i['title'] for i in r['items']]==['fresh']


def test_public_news_any_tagged_user(monkeypatch):
    import cr_web as W
    monkeypatch.setattr(W,'news',lambda *a:{'items':[]})
    assert 'No dated news results' in G.answer({'text':'@crayon_v1_bot latest news','from':{'id':12}})


def test_group_news_clean_no_links(monkeypatch):
    import cr_web as W
    monkeypatch.setattr(W,'news',lambda *a:{'items':[{'title':'Clear headline','url':'https://news.google.com/test','source':'Publisher','published':'2026-10-09T10:00:00+05:30'}],'day':None})
    r=G.answer({'text':'@crayon_v1_bot latest news','from':{'id':12}})
    assert '1. Clear headline' in r and 'Publisher |' in r and 'https://' not in r and not hasattr(r,'markup')


def test_group_explicit_date_count_topic(monkeypatch):
    import cr_web as W
    from datetime import date
    calls=[];monkeypatch.setattr(W,'news',lambda q,n,d:calls.append((q,n,d)) or {'items':[]})
    G.answer({'text':'@crayon_v1_bot give top 5 news for Portugal from 8th Oct 2026','from':{'id':12}})
    assert calls==[('Portugal',5,date(2026,10,8))]


def test_group_portugal_clean_topic(monkeypatch):
    import cr_web as W
    calls=[];monkeypatch.setattr(W,'news',lambda q,n,d:calls.append(q) or {'items':[]})
    G.answer({'text':'@crayon_v1_bot okay give top 5 news for Portugal','from':{'id':12}})
    G.answer({'text':'@crayon_v1_bot can you give top 5 news from Portugal and Venezuela','from':{'id':12}})
    assert calls==['Portugal','Portugal','Venezuela']

def test_news_relevance_publisher_dedupe(monkeypatch):
    import cr_web as W,httpx
    from datetime import date
    xml=b'<rss><channel><item><title>Audio briefing - Publisher</title><source>Publisher</source><pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate><link>https://news.google.com/a</link></item><item><title>Portugal policy - Publisher</title><source>Publisher</source><pubDate>Thu, 08 Oct 2026 10:00:00 GMT</pubDate><link>https://news.google.com/b</link></item></channel></rss>'
    monkeypatch.setattr(W._c,'get',lambda *a,**kw:httpx.Response(200,content=xml))
    r=W.news('Portugal',5,date(2026,10,8));assert [i['title'] for i in r['items']]==['Portugal policy']


def test_group_direct_email_routes_requester_not_chat(monkeypatch):
    calls=[]
    import cr_group_actions as GA
    monkeypatch.setattr(GA,'consent',lambda *a:True)
    monkeypatch.setattr(T,'_handle_text',lambda uid,chat,name,text,mid,out:calls.append((uid,chat,text)))
    T.handle_update({'message':{'chat':{'id':-991,'type':'group'},'from':{'id':22},'text':'@crayon_v1_bot email Sam saying hello'}},T.CaptureOut())
    assert calls==[(22,-991,'email Sam saying hello')]


def test_group_forward_anonymous_and_connect_are_explicit(monkeypatch):
    monkeypatch.setattr(T,'_handle_text',lambda *a:(_ for _ in ()).throw(AssertionError('private route')))
    for extra,text in [({'forward_origin':{'type':'user'}},'email Sam'),({'sender_chat':{'id':-1}},'email Sam'),({},'connect Google')]:
        out=T.CaptureOut()
        T.handle_update({'message':{'chat':{'id':-991,'type':'group'},'from':{'id':22},'text':'@crayon_v1_bot '+text,**extra}},out)
        assert len(out.sent)==1


def test_group_controls_requester_and_chat_bound(monkeypatch):
    import cr_group_actions as A
    store={}
    monkeypatch.setattr(A.db,'kv_set',lambda k,v:store.__setitem__(k,v))
    monkeypatch.setattr(A.db,'kv_get',lambda k,d=None:store.get(k,d))
    out=A.GroupOut(T.CaptureOut(),22,-991)
    out.send(-991,'Reviewed',{'inline_keyboard':[[{'text':'Send','callback_data':'email_send:a:b'}]]})
    assert A.reviewed(22,-991,'email_send:a:b')
    assert not A.reviewed(23,-991,'email_send:a:b')
    assert not A.reviewed(22,-992,'email_send:a:b')
    assert not A.reviewed(22,-991,'email_send:x:y')


def test_group_other_member_callback_no_effect(monkeypatch):
    import cr_group_actions as A
    monkeypatch.setattr(T,'api',lambda *a,**k:None)
    monkeypatch.setattr(A,'reviewed',lambda *a:False)
    out=T.CaptureOut()
    T.handle_callback({'id':'cb','data':'email_send:a:b','from':{'id':23},'message':{'chat':{'id':-991}}},out)
    assert not out.sent


def test_group_audience_consent_before_any_data(monkeypatch):
    import cr_group_actions as A
    values={}
    monkeypatch.setattr(A.db,'kv_get',lambda k,d=None:values.get(k,d))
    monkeypatch.setattr(A.db,'kv_set',lambda k,v:values.__setitem__(k,v))
    out=T.CaptureOut()
    assert not A.consent(22,-991,'read my inbox',out)
    assert 'everyone' in out.sent[0]['text'] and values=={}
    assert not A.consent(22,-991,'enable my group actions',out)
    assert A.consent(22,-991,'read my inbox',out)
    assert not A.consent(23,-991,'read my inbox',out)
    assert not A.consent(22,-992,'read my inbox',out)


def test_group_optin_phrase_routes_to_consent():
    import cr_group_actions as A
    assert A.action_request('enable my group actions')


def test_group_proactive_cannot_move_private_alerts(monkeypatch):
    monkeypatch.setattr(T.db,'audit',lambda *a:None)
    out=T.CaptureOut();T._handle_text(22,-991,'Test','turn on daily check-ins',None,out)
    assert 'private monitoring' in out.sent[0]['text']


def test_group_followups_keep_full_review_flow():
    import cr_group_actions as A
    for text in ('send it','cancel draft','sam@example.com','read first','yes'):
        assert A.action_request(text)
