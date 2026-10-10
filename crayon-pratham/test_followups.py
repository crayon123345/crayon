import cr_followups as F

def test_followup_explicit_and_bounded(monkeypatch):
    class Out:
        def send(self,c,t):self.text=t
    out=Out();F.handle(1,1,'/followup 0h presentation',out)
    assert '1-168' in out.text
    F.handle(1,-2,'/followup 24h presentation',out)
    assert 'private' in out.text

def test_quiet_hour_deferral(monkeypatch):
    import cr_proactive as P
    from datetime import datetime
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(F.T,'now_local',lambda uid:datetime(2026,10,10,20,30,tzinfo=ZoneInfo('Asia/Calcutta')))
    monkeypatch.setattr(P,'settings',lambda uid:{'quiet_start':21,'quiet_end':9})
    calls=[]
    monkeypatch.setattr(F.T,'set_reminder',lambda ctx,**kw:calls.append(kw) or {'verified':True,'due_local':'next awake window'})
    class Out:
        def send(self,*a):pass
    F.handle(1,1,'/followup 1h presentation',Out())
    assert calls[0]['in_minutes']==780 and 'How did presentation go?'==calls[0]['text'].split(' Want')[0]
