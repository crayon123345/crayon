import cr_work as W
import pytest

def test_arithmetic_no_code():
    assert W.calculate('20*(3+2)/4')==25
    for bad in ('__import__("os")','2**100','sum([1,2])','1/0','[1]'):
        with pytest.raises(Exception):W.calculate(bad)

def test_plan_scope():
    title,steps=W.parse('Demo | calculate 2+2 | calculate 4*5')
    assert title=='Demo' and len(steps)==2
    for bad in ('send hello','email a@b.com','Demo | calculate 1 | calculate 2 | calculate 3 | calculate 4'):
        with pytest.raises(ValueError):W.parse(bad)

def test_evidence_page(monkeypatch):
    import cr_web
    monkeypatch.setattr(cr_web,'fetch',lambda *a:{'url':'https://example.com','title':'Example','text':'verified page'})
    r=W.step_run({'op':'page','input':'https://example.com'})
    assert r['sources']==['https://example.com'] and 'verified page' in r['text']

def test_no_sources_no_done(monkeypatch):
    import cr_web
    monkeypatch.setattr(cr_web,'research',lambda *a:{'pages':[]})
    with pytest.raises(ValueError):W.step_run({'op':'research','input':'demo'})

def test_job_view_honest():
    text=W.view({'id':2,'title':'Demo','status':'blocked','steps':[{'op':'calculate','input':'2+2'}],'results':[]})
    assert 'not completed' in text and '0/1' in text and 'Stopped' in text

def test_cross_user_control(monkeypatch):
    monkeypatch.setattr(W,'get',lambda *a:None)
    with pytest.raises(ValueError):W.control(10,2,'resume')

def test_not_auto_activate():
    class Out:
        def send(self,*a,**k):pytest.fail('no command')
    assert not W.handle(1,1,'do more automatically',Out())

def test_scheduled_writes_blocked():
    import cr_tools as T
    for tool,args in [('remember',{'key':'x','value':'y'}),('create_csv',{'headers':['x'],'rows':[['1']]}),('computer_browse',{'url':'https://example.com'})]:
        r=T.run(tool,args,{'uid':1,'readonly':True,'meta':{}})
        assert not r['ok'] and 'read-only' in r['error']
    names=[x['name'] for x in T.declarations(readonly=True)[0]['functionDeclarations']]
    assert 'research_web' in names and 'remember' not in names and 'computer_browse' not in names

def test_dashboard_no_invented_done(monkeypatch):
    import cr_dashboard as D
    monkeypatch.setattr(D.T,'list_tasks',lambda ctx:{'tasks':[{'id':4,'title':'Demo','done':0,'total':2,'steps':[{'n':1,'title':'Review','status':'blocked'},{'n':2,'title':'Write','status':'todo'}]}]})
    monkeypatch.setattr(D.T,'list_reminders',lambda ctx:{'reminders':[]})
    monkeypatch.setattr(W,'init',lambda:None)
    monkeypatch.setattr(D.db,'q',lambda *a:[])
    text=D.render(1)
    assert 'Blocked: Review' in text and 'Next 2: Write' in text and '0/2' in text

def test_brief_rejects_invented_link(monkeypatch):
    import cr_web,cr_llm
    monkeypatch.setattr(cr_web,'research',lambda *a:{'pages':[{'url':'https://example.com','text':'Evidence'}]})
    monkeypatch.setattr(cr_llm,'ask_json',lambda *a,**k:{'points':[{'claim':'fake','url':'https://evil.invalid'}]})
    with pytest.raises(ValueError):W.step_run({'op':'brief','input':'topic'})

def test_brief_source_bound(monkeypatch):
    import cr_web,cr_llm
    monkeypatch.setattr(cr_web,'research',lambda *a:{'pages':[{'url':'https://example.com','text':'Evidence'}]})
    monkeypatch.setattr(cr_llm,'ask_json',lambda *a,**k:{'answer':'Draft','points':[{'claim':'point','url':'https://example.com'}],'outline':['Intro'],'gaps':['Check']})
    r=W.step_run({'op':'brief','input':'topic'})
    assert r['draft'] and 'check before using' in r['text'] and 'https://example.com' in r['text']

def test_pause_running_cannot_resume(monkeypatch):
    row={'id':1,'user_id':1,'status':'running','steps':[{}],'results':[]};written=[]
    monkeypatch.setattr(W,'get',lambda *a:row)
    def q(sql,args,fetch):written.append(args);row['status']=args[0]
    monkeypatch.setattr(W.db,'q',q)
    W.control(1,1,'pause')
    assert row['status']=='pausing'
    with pytest.raises(ValueError):W.control(1,1,'resume')

def test_task_add_deterministic(monkeypatch):
    import cr_dashboard as D
    called=[]
    monkeypatch.setattr(D.T.mem,'touch_user',lambda *a:None)
    monkeypatch.setattr(D.T,'create_task',lambda ctx,title,steps:called.append((title,steps)) or {'verified':True})
    monkeypatch.setattr(D,'render',lambda uid:'dashboard')
    class Out:
        def send(self,*a):pass
    assert D.handle(1,1,'/tasks add Demo | Read | Write',Out())
    assert called==[('Demo',['Read','Write'])]

def test_startup_off_no_credential_access():
    import subprocess,os
    env={**os.environ,'CRAYON_COMPUTER_AUTOSTART':'off'}
    r=subprocess.run(['sh','computer_start.sh'],env=env,capture_output=True)
    assert r.returncode==0 and not r.stdout and not r.stderr

def test_quiet_hours_defer(monkeypatch):
    from datetime import datetime
    row={'id':1,'user_id':1,'chat_id':1,'status':'done','notified':False,'title':'Demo','steps':[{'op':'calculate','input':'2+2'}],'results':[{'text':'4'}]}
    calls=[]
    def q(sql,args=(),fetch='all'):
        calls.append(sql)
        if sql.startswith('SELECT * FROM work_jobs'):return [row]
        return None
    monkeypatch.setattr(W.db,'q',q)
    monkeypatch.setattr(W.P,'settings',lambda uid:{'quiet_start':21,'quiet_end':9})
    monkeypatch.setattr(W.P.T,'now_local',lambda uid:datetime(2026,10,9,2))
    class Out:
        def send(self,*a):pytest.fail('quiet ping')
    W.tick(Out(),only_user=1)
    assert not any('notified=true' in x for x in calls)

def test_decimal_calculation():
    from decimal import Decimal
    assert W.calculate('0.1+0.2')==Decimal('0.3')
    assert str(W.calculate('1/3'))=='0.3333333333333333333333333333'

def test_work_buttons_state_scoped():
    paused=W.controls_markup({'id':12,'status':'paused','results':[]})
    flat=[b['callback_data'] for row in paused['inline_keyboard'] for b in row]
    assert 'work:resume:12' in flat and 'work:pause:12' not in flat
    done=W.controls_markup({'id':12,'status':'done','results':[{}]})
    flat=[b['callback_data'] for row in done['inline_keyboard'] for b in row]
    assert 'work:export:12' in flat and 'work:cancel:12' not in flat

def test_uncertain_delivery_not_retried(monkeypatch):
    from datetime import datetime
    import contextlib
    row={'id':1,'user_id':1,'chat_id':1,'status':'done','notified':False,'title':'Demo','steps':[{'op':'calculate','input':'2+2'}],'results':[{'text':'4'}]}
    sqls=[]
    def q(sql,args=(),fetch='all'):
        sqls.append(sql)
        if sql.startswith('SELECT * FROM work_jobs'):return [row] if not row['notified'] else []
        if 'RETURNING id' in sql and 'notified=true' in sql:row['notified']=True;return {'id':1}
        return None
    monkeypatch.setattr(W.db,'q',q);monkeypatch.setattr(W,'get',lambda *a:row.copy())
    monkeypatch.setattr(W.P,'settings',lambda uid:{'quiet_start':21,'quiet_end':9})
    monkeypatch.setattr(W.P.T,'now_local',lambda uid:datetime(2026,10,9,12))
    monkeypatch.setattr(W.P.mem,'user_lock',lambda uid:contextlib.nullcontext())
    class Out:
        calls=0
        def send(self,*a,**k):self.calls+=1;raise TimeoutError()
    out=Out();W.tick(out,only_user=1);W.tick(out,only_user=1)
    assert out.calls==1 and any("delivery_state='uncertain'" in s for s in sqls)

def test_callback_cross_user_private_gate(monkeypatch):
    import cr_telegram as Tg
    monkeypatch.setattr(Tg,'api',lambda *a,**k:None)
    monkeypatch.setattr(W,'handle',lambda *a,**k:pytest.fail('wrong chat'))
    Tg.handle_callback({'id':'test','data':'work:show:1','from':{'id':10},'message':{'chat':{'id':20}}},Tg.CaptureOut())

def test_work_callback_routes_exact(monkeypatch):
    import cr_telegram as Tg,contextlib
    called=[]
    monkeypatch.setattr(Tg,'api',lambda *a,**k:None)
    monkeypatch.setattr(Tg.mem,'user_lock',lambda *a:contextlib.nullcontext())
    monkeypatch.setattr(W,'handle',lambda *a:called.append(a))
    Tg.handle_callback({'id':'test','data':'work:pause:12','from':{'id':10},'message':{'chat':{'id':10}}},Tg.CaptureOut())
    assert called[0][0:3]==(10,10,'/work pause 12')

def test_resume_finished_pause_goes_done(monkeypatch):
    row={'id':1,'status':'paused','steps':[{}],'results':[{}]}
    monkeypatch.setattr(W,'get',lambda *a:row)
    def q(sql,args,fetch):row['status']=args[0]
    monkeypatch.setattr(W.db,'q',q)
    assert W.control(1,1,'resume')['status']=='done'

def test_failed_pause_no_retry(monkeypatch):
    monkeypatch.setattr(W,'get',lambda *a:{'status':'paused','error':'timed out'})
    with pytest.raises(ValueError):W.control(1,1,'resume')
