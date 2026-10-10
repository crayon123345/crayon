import cr_agent as A
import cr_tools as T
import cr_selftest

def test_positive_selftest_rejected():
    assert not cr_selftest.run({'uid':123})['ok']

def test_invalid_tool():
    assert not T.run('send_email', {}, {'uid':-1})['verified']

def test_goal_bad_plan(monkeypatch):
    monkeypatch.setattr(A.mem,'touch_user',lambda *a:None)
    monkeypatch.setattr(A.mem,'add_message',lambda *a:None)
    monkeypatch.setattr(A,'handle_confirmation',lambda *a:None)
    monkeypatch.setattr(A,'_history_contents',lambda *a:[])
    monkeypatch.setattr(A,'build_system',lambda *a:'test')
    monkeypatch.setattr(A.llm,'ask_json',lambda *a,**k:{'steps':[]})
    reply, meta = A.respond(-1,-1,'Research X',goal_mode=True)
    assert 'safe plan' in reply

def test_media_rejects_large_and_unsupported():
    import pytest, cr_media
    assert "can't decode" in cr_media.analyze(b'x','application/zip')
    with pytest.raises(ValueError):
        cr_media.analyze(b'x'*(cr_media.MAX_BYTES+1),'image/png')

def test_media_uses_inline_gemini(monkeypatch):
    import cr_media
    def fake(contents, **kwargs):
        assert contents[0]['parts'][0]['inlineData']['mimeType']=='image/png'
        assert kwargs.get('tools') is None
        return {'text':'A red square.'}
    monkeypatch.setattr(cr_media.llm,'generate',fake)
    assert cr_media.analyze(b'png','image/png')=='A red square.'

def test_goal_executes_verified_steps(monkeypatch):
    monkeypatch.setattr(A.mem,'touch_user',lambda *a:None)
    monkeypatch.setattr(A.mem,'add_message',lambda *a:None)
    monkeypatch.setattr(A.mem,'extract_async',lambda *a:None)
    monkeypatch.setattr(A,'handle_confirmation',lambda *a:None)
    monkeypatch.setattr(A,'_history_contents',lambda *a:[])
    monkeypatch.setattr(A,'build_system',lambda *a:'test')
    monkeypatch.setattr(A.llm,'ask_json',lambda *a,**k:{'steps':['Calculate','Save']})
    calls = []
    def run(name,args,ctx):
        calls.append(name)
        return {'ok':True,'verified':True,'task':{'id':99,'steps':[]}}
    monkeypatch.setattr(A.T,'run',run)
    results = iter([{'calls':[{'name':'run_python','args':{'task':'2+2'}}], 'parts':[], 'model':'test'},
                    {'calls':[], 'text':'The answer is 4.', 'model':'test'}])
    monkeypatch.setattr(A.llm,'generate',lambda *a,**k:next(results))
    reply, meta = A.respond(-1,-1,'Calculate 2+2',goal_mode=True)
    assert calls==['create_task','run_python']
    assert meta['trace'][0]['verified'] and 'Plan:' in reply

def test_quiet_hours():
    from cr_proactive import awake
    assert not awake(23,21,9)
    assert not awake(8,21,9)
    assert awake(9,21,9)
    assert awake(20,21,9)
    assert not awake(14,12,16)


def test_memory_review_changes_no_facts(monkeypatch):
    import cr_memory as M
    monkeypatch.setattr(M,'facts',lambda *a:[{'key':'color','value':'Blue'},{'key':'favorite_color','value':'blue'}])
    monkeypatch.setattr(M.db,'q',lambda *a:[])
    monkeypatch.setattr(M.llm,'ask_json',lambda *a,**k:{'suggestions':[]})
    monkeypatch.setattr(M,'maybe_summarize',lambda *a:None)
    r=M.review_memory(-1)
    assert r['duplicates']==[['color','favorite_color']]
    assert not r['suggestions']


def test_draft_never_sends(monkeypatch):
    import cr_llm
    monkeypatch.setattr(cr_llm,'generate',lambda *a,**k:{'text':'Hi Sam, could we move the meeting?'})
    r=T.draft_message({'uid':-1},'Sam','Ask to move meeting')
    assert r['sent'] is False and r['verified']

def test_digest_once_and_proactive_once(monkeypatch):
    import cr_proactive as P
    from datetime import datetime, timezone
    settings={'proactive':True,'digest':'both','quiet_start':0,'quiet_end':0,'chat_id':-4}
    def query(sql,params=(),fetch='all'):
        if sql.startswith('SELECT user_id,settings'):
            return [{'user_id':-4,'settings':settings.copy()}]
        if sql.startswith('SELECT id,title FROM tasks'):
            return None
        if sql.startswith('SELECT text FROM reminders'):
            return {'text':'Test reminder'}
        if sql.startswith('UPDATE users SET settings=jsonb_set'):
            import json
            settings[params[0][0]]=json.loads(params[1])
            return None
        raise AssertionError(sql)
    monkeypatch.setattr(P.db,'q',query)
    monkeypatch.setattr(P.T,'now_local',lambda uid:datetime(2026,10,8,10,tzinfo=timezone.utc))
    monkeypatch.setattr(P,'digest_text',lambda uid:'Digest test')
    from cr_telegram import CaptureOut
    out=CaptureOut()
    assert P.tick(out,only_user=-4)==['digest_morning','proactive_date']
    assert P.tick(out,only_user=-4)==[]
    assert len(out.sent)==2


def test_quiet_hours_block_all_optional_messages(monkeypatch):
    import cr_proactive as P
    from datetime import datetime,timezone
    monkeypatch.setattr(P.db,'q',lambda *a:[{'user_id':-4,'settings':{'proactive':True,'digest':'both','chat_id':-4}}])
    monkeypatch.setattr(P.T,'now_local',lambda uid:datetime(2026,10,8,23,tzinfo=timezone.utc))
    from cr_telegram import CaptureOut
    out=CaptureOut()
    assert P.tick(out,only_user=-4)==[] and not out.sent

def test_reaction_selection():
    from cr_reactions import choose
    assert choose('Please build this')=='👀'
    assert choose('Lets gooo')=='🔥'
    assert choose('thank you')=='❤️'
    assert choose('I passed the exam')=='🎉'
    assert choose('lol haha')=='😂'
    assert choose('What time is it?')=='🤔'
    assert choose('yes') is None
    assert choose('/memory') is None
