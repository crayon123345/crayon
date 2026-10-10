import pytest
import cr_web_notifications as N
import cr_web_app as W
import cr_sched as S
from datetime import datetime,timezone

def test_private_notification_reads_and_ack(monkeypatch):
    calls=[]
    def q(sql,p=(),*a,**kw):
        calls.append((sql,p))
        if sql.startswith('SELECT id,'):return [{'id':'a'*64,'encrypted':'x','created_at':'now','seen':False}]
    monkeypatch.setattr(N.db,'q',q);monkeypatch.setattr(W,'decode',lambda value:{'text':'private'})
    assert N.list_for(12)[0]['text']=='private'
    N.acknowledge(13,'a'*64)
    assert calls[-1][1]==('a'*64,13)
    assert any('WHERE user_id=%s' in sql and p==(12,) for sql,p in calls)
    with pytest.raises(ValueError):N.acknowledge(12,'invalid')

def test_publication_dedup_account_binding(monkeypatch):
    calls=[]
    monkeypatch.setattr(N.db,'q',lambda sql,p=(),*a,**kw:calls.append((sql,p)) or ({'user_id':12} if sql.startswith('SELECT user_id') else None))
    monkeypatch.setattr(W,'encode',lambda value:'encrypted')
    assert N.publish(12,'test','key')==N.publish(12,'test','key')
    assert N.publish(13,'test','key')!=N.publish(12,'test','key')
    assert any('SELECT %s,user_id,%s FROM users WHERE user_id=%s' in sql for sql,p in calls)

def test_standalone_scheduler_never_phones(monkeypatch):
    now=datetime.now(timezone.utc)
    row={'id':1,'user_id':10**15,'chat_id':10**15,'text':'test','due_at':now,'recurrence':''}
    def q(sql,p=(),*a):
        if sql.startswith('UPDATE reminders SET status=\'sending\''):return [row]
        if sql.startswith('SELECT now()'):return {'n':now}
    monkeypatch.setattr(S.db,'q',q)
    monkeypatch.setattr(__import__('cr_accounts'),'telegram_destination',lambda uid:None)
    calls=[];monkeypatch.setattr(N,'publish',lambda *a:calls.append(a))
    class Out:
        def send(self,*a):pytest.fail('No invented Telegram destination')
    assert S.tick(Out())==[1] and calls[0][0]==10**15

def test_web_computer_and_work_dispatch(monkeypatch):
    import cr_memory as M,cr_computer as K,cr_work as Q
    monkeypatch.setattr(W.db,'q',lambda *a,**kw:{'user_id':12})
    monkeypatch.setattr(M,'touch_user',lambda *a:None)
    monkeypatch.setattr(K,'status',lambda uid:{'ok':True,'user':uid})
    monkeypatch.setattr(Q,'handle',lambda uid,chat,text,out:out.send(chat,'work_'+str(uid)))
    assert '12' in W.dispatch(12,'User','/computer status')[0]['text']
    assert W.dispatch(12,'User','/work list')[0]['text']=='work_12'

def test_proactive_web_review_validation(monkeypatch):
    import cr_proactive as P
    monkeypatch.setattr(P.db,'q',lambda *a,**k:pytest.fail('DB before validation'))
    for body in ({},{'proactive':True,'digest':'both','quiet_start':21,'quiet_end':9,'accept':False},{'proactive':True,'digest':'both','quiet_start':True,'quiet_end':9,'accept':True}):
        with pytest.raises(ValueError):P.web_update(1000000000000001,body)

def test_standalone_proactive_private_inapp(monkeypatch):
    import cr_proactive as P,cr_web_notifications as N
    from datetime import datetime,timezone
    uid=1000000000000001
    settings={'proactive':False,'digest':'morning','delivery':'web','quiet_start':0,'quiet_end':0}
    def q(sql,params=(),*args,**kw):
        if sql.startswith('SELECT user_id,settings'):return [{'user_id':uid,'settings':settings.copy()}]
        if sql.startswith('UPDATE users SET settings=jsonb_set'):
            import json
            settings[params[0][0]]=json.loads(params[1]);return
        raise AssertionError(sql)
    monkeypatch.setattr(P.db,'q',q)
    monkeypatch.setattr(P.T,'now_local',lambda uid:datetime(2026,10,10,10,tzinfo=timezone.utc))
    monkeypatch.setattr(P,'digest_text',lambda uid:'Private digest')
    sent=[];monkeypatch.setattr(N,'publish',lambda *a:sent.append(a))
    class Out:
        def send(self,*a):pytest.fail('No invented Telegram delivery')
    assert P.tick(Out())==['digest_morning']
    assert P.tick(Out())==[]
    assert sent==[(uid,'Private digest','proactive:digest_morning:2026-10-10')]
