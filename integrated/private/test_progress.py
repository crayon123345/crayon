import threading
import cr_telegram as T

def test_no_progress_after_reply():
    out=T.CaptureOut();guard=T.ProgressOut(out,1,[])
    assert guard.emit('working')
    guard.send(1,'final')
    assert not guard.emit('late')
    assert [x['text'] for x in out.sent]==['working','final']

def test_inflight_progress_serialized_before_final():
    entered=threading.Event();release=threading.Event()
    class SlowOut(T.CaptureOut):
        def send(self,chat,text,markup=None):
            if text=='working':
                entered.set();assert release.wait(2)
            super().send(chat,text,markup)
    out=SlowOut();guard=T.ProgressOut(out,1,[])
    worker=threading.Thread(target=guard.emit,args=('working',));worker.start()
    assert entered.wait(2)
    final=threading.Thread(target=guard.send,args=(1,'final'));final.start()
    release.set();worker.join(2);final.join(2)
    assert not worker.is_alive() and not final.is_alive()
    assert not guard.emit('late')
    assert [x['text'] for x in out.sent]==['working','final']
import cr_progress as P
import cr_tools as Tools
import cr_web_app as W

def test_progress_is_context_bound_and_no_arguments(monkeypatch):
    seen=[];token=P.callback.set(seen.append)
    try:
        monkeypatch.setattr(Tools,'now_local',lambda uid:__import__('datetime').datetime(2026,10,10))
        Tools.run('get_time',{}, {'uid':12,'meta':{}})
        assert seen==[{'label':'Running a requested tool','state':'running'},{'label':'Running a requested tool','state':'done'}]
    finally:P.callback.reset(token)
    P.emit('Should not leak into another request')
    assert len(seen)==2

def test_result_progress_is_owner_scoped(monkeypatch):
    calls=[]
    monkeypatch.setattr(W.db,'q',lambda sql,p,fetch:calls.append(p) or {'state':'running','updated_at':__import__('datetime').datetime.now(__import__('datetime').timezone.utc),'progress':[{'label':'Reading a public page','state':'running'}]})
    assert W.result(12,'a'*43)['progress'][0]['label']=='Reading a public page'
    assert calls==[(12,'a'*43)]
