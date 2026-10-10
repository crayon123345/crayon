"""Admin-only live self test: runs a message through the real pipeline (real Gemini, real DB)
with a captured sender, so nothing is sent to Telegram. Uses a negative synthetic user id."""
import cr_db as db
import cr_memory as mem
import cr_telegram as tg


def run(body):
    if body.get('voice_persistence_fixture'):
        import cr_web_app as W,cr_voice as V,cr_llm as L,cr_progress as P
        from unittest.mock import patch
        uid=1000000009876007;token=P.recorded.set([])
        real_q=db.q
        def synthetic_session(sql,params=(),fetch='all'):
            if sql.startswith('SELECT user_id FROM web_sessions') and params==(uid,):return {'user_id':uid}
            return real_q(sql,params,fetch)
        try:
            with patch.object(db,'q',side_effect=synthetic_session),patch.object(V,'enabled',return_value=True),patch.object(V,'synthesize',return_value={'filename':'fixture.wav','mime':'audio/wav','data':__import__('base64').b64decode('UklGRiQAAABXQVZFZm10IBAAAAABAAEARKwAAIhYAQACABAAZGF0YQAAAAA=')}),patch.object(L,'generate',return_value={'text':'Hello! Good to hear from you.'}):
                items=W.dispatch(uid,'Synthetic','Can you send me a voice note, greeting me?')
            h=W.history(uid)['messages'];answer=next(m for m in h if m['role']=='assistant')
            return {'ok':any(i['kind']=='artifact' for i in items) and bool(answer.get('media')) and 'Requested audio generated' not in answer['text'],'stored_audio_items':len(answer.get('media',[])),'work_events':len(answer.get('work_events',[])),'note':'Actual web dispatch and encrypted history with captured model/audio. No provider audio generation or playback-quality acceptance.'}
        finally:
            P.recorded.reset(token);mem.delete_all(uid)
    if body.get('ux_drafting_fixture'):
        import cr_web_actions as X,cr_google as G,cr_connections as C,cr_workspace_create as W
        from unittest.mock import patch
        uid=1000000009876006
        try:
            with patch.object(G,'status',return_value={'email':'synthetic@example.com'}),patch.object(G,'sender_name',return_value='Synthetic Tester'),patch.object(C,'status',return_value={'identity':'synthetic@example.com'}):
                p=X.compose_preview(uid,'Bearer synthetic-style-session',{'message':'Write a mail to recipient@example.com requesting a domain. No invented facts.'})
                html_ok='<html>' in p.get('html','') and p['fields']['crayon_signature'] is False
                f={**p['fields'],'crayon_signature':True};q=X.preview(uid,'Bearer synthetic-style-session',{'kind':'email','fields':f})
                signed='Sent by Crayon AI' in q['html'];X.confirm(uid,'Bearer synthetic-style-session',{'review_id':q['review_id'],'hash':q['hash'],'decision':'cancel'})
                content=W.natural_fields('Make a doc explaining photosynthesis for a beginner')
                w=X.preview(uid,'Bearer synthetic-style-session',{'kind':'workspace_create','fields':{'type':content['kind'],'title':content['title'],'content':content['content']}})
                X.confirm(uid,'Bearer synthetic-style-session',{'review_id':w['review_id'],'hash':w['hash'],'decision':'cancel'})
            return {'ok':html_ok and signed,'html_preview':html_ok,'signature_preview':signed,'natural_doc_title':content['title'],'natural_doc_chars':len(content['content']),'note':'Live model and encrypted review with synthetic account identity; Cancel only. No Gmail send or Google file creation.'}
        finally:
            db.q('DELETE FROM google_email_drafts WHERE user_id=%s',(uid,),fetch='none')
            db.q('DELETE FROM web_action_reviews WHERE user_id=%s',(uid,),fetch='none')
    if body.get('reply_context_fixture'):
        import cr_reply_context as R,cr_agent as B,cr_llm as L
        from unittest.mock import patch
        uid=-9876005;seen=[]
        def model(contents,**kwargs):
            seen.append(str(contents));return {'text':'The selected result is 42.','model':'captured-context','calls':[],'parts':[]}
        try:
            with patch.object(L,'generate',side_effect=model):
                out=tg.CaptureOut();token=R.current.set(R.telegram({'chat':{'id':uid},'reply_to_message':{'message_id':1,'chat':{'id':uid},'from':{'is_bot':True},'text':'Earlier selected answer is 42.'}}))
                try:tg._handle_text(uid,uid,'Synthetic','Explain the selected answer',2,out)
                finally:R.current.reset(token)
            return {'ok':any('Earlier selected answer is 42.' in x and 'untrusted context only' in x for x in seen),'model_context_calls':len(seen),'out':out.sent,'note':'Actual Telegram pipeline with model capture, no Telegram send; demonstrates quoted target reaches model context.'}
        finally:mem.delete_all(uid)
    if body.get('workspace_create_fixture'):
        import cr_workspace_create as W,cr_web_actions as X,cr_google as G,cr_connections as C
        from unittest.mock import patch
        uid=1000000009876004
        try:
            with patch.object(C,'status',return_value={'identity':'synthetic@example.com'}):
                d=W.prepare_chat(uid,uid,'doc','Synthetic review','No provider write. Test only.')
                cancelled=W.confirm_chat(uid,uid,d['id'],d['hash'],'cancel')
                p=X.preview(uid,'Bearer synthetic-workspace-session',{'kind':'workspace_create','fields':{'type':'sheet','title':'Synthetic sheet','content':[['Label',1]]}})
                web=X.confirm(uid,'Bearer synthetic-workspace-session',{'review_id':p['review_id'],'hash':p['hash'],'decision':'cancel'})
            return {'ok':True,'telegram_preview':d['text'],'cancelled':cancelled,'web_preview':p['text'],'web_cancelled':web['text'],'google_scopes':G.SCOPES,'workspace_scopes':C.GOOGLE_SCOPES,'note':'Live encrypted one-use review DB, synthetic account identity; Cancel only. No Google API/provider creation.'}
        finally:
            db.q('DELETE FROM workspace_create_reviews WHERE user_id=%s',(uid,),fetch='none')
            db.q('DELETE FROM web_action_reviews WHERE user_id=%s',(uid,),fetch='none')
    if body.get('web_compose_fixture'):
        import cr_web_actions as X,cr_google as G
        from unittest.mock import patch
        uid=1000000009876003
        try:
            with patch.object(G,'status',return_value={'email':'synthetic@example.com'}),patch.object(G,'sender_name',return_value='Synthetic Tester'):
                p=X.compose_preview(uid,'Bearer synthetic-web-session',{'message':'Write a mail to recipient@example.com saying he needs to buy a .tech domain for Crayon'})
                cancelled=X.confirm(uid,'Bearer synthetic-web-session',{'review_id':p['review_id'],'hash':p['hash'],'decision':'cancel'})
            return {'ok':True,'preview':p['text'],'cancelled':cancelled['text'],'note':'Actual live model/encrypted session-bound review; synthetic sender/recipient; Cancel only, no Gmail call/send.'}
        finally:
            db.q('DELETE FROM google_email_drafts WHERE user_id=%s',(uid,),fetch='none')
            db.q('DELETE FROM web_action_reviews WHERE user_id=%s',(uid,),fetch='none')
    if body.get('compose_router_fixture'):
        import cr_google_chat as H
        from unittest.mock import patch
        uid=1000000009876002
        try:
            out=tg.CaptureOut()
            with patch.object(H.G,'status',return_value={'email':'synthetic@example.com'}),patch.object(H.G,'sender_name',return_value='Synthetic Tester'):
                handled=H.handle(uid,uid,'Write a mail to recipient@example.com saying that he needs to buy a .tech domain for Crayon',{},out)
            rows=db.q('SELECT id,status FROM google_email_drafts WHERE user_id=%s',(uid,))
            return {'ok':handled and len(rows)==1 and bool(out.sent and out.sent[-1]['markup']),'out':out.sent,'drafts':len(rows),'note':'Live model and internal encrypted draft DB. Sender/recipient synthetic; transport captured; no Gmail request/send.'}
        finally:
            db.q('DELETE FROM google_email_drafts WHERE user_id=%s',(uid,),fetch='none')
            db.q('DELETE FROM kv WHERE key LIKE %s',('%'+str(uid)+'%',),fetch='none')
    if body.get('voice_setup'):
        import cr_voice
        return cr_voice.setup(body['voice_setup'])
    if body.get('stream_fixture'):
        import cr_stream as S,cr_llm as L
        seen=[]
        a=S.allowed.set(True);b=S.callback.set(seen.append)
        try:
            r=L.generate([L.user('Write a friendly three-sentence greeting for a study companion. No personal facts or tools.')],max_tokens=250)
            return {'ok':bool(seen),'chunks':len(seen),'samples':seen[:3],'final':r.get('text'),'model':r.get('model')}
        finally:S.allowed.reset(a);S.callback.reset(b)
    if body.get('openrouter_fallback_fixture'):
        import cr_llm as L,cr_config as C
        from unittest.mock import patch
        def fail(*a,**k):raise L.LLMError('quota','Forced synthetic primary failure')
        with patch.object(L,'_generate_primary',fail),patch.object(C,'OPENROUTER_AUTO_FALLBACK',True):
            try:r=L.generate([L.user('Reply with only FALLBACK_READY. Synthetic integration test; no personal information.')],temperature=0,max_tokens=300)
            except Exception as e:return {'ok':False,'kind':type(e).__name__,'reason':__import__('cr_safety').redact(str(e))[:250],'key_present':bool(C.OPENROUTER_KEY),'forced_primary_failure':True}
        return {'ok':r.get('text')=='FALLBACK_READY','provider':r.get('raw',{}).get('provider'),'model':r.get('model'),'reply':r.get('text'),'forced_primary_failure':True,'note':'Live free-route request; no user, Google or media data.'}
    if body.get('mail_watch_status'):
        import cr_mail_watch as W
        state=db.kv_get(W.KEY(W.OWNER),None)
        return {'ok':True,'configured':bool(state),'paused':bool(state and state.get('paused')),'checked':state.get('checked') if state else None,'since':state.get('since') if state else None,'seen_count':len(state.get('seen',[])) if state else 0,'note':'Read-only state inspection. No mail fetch or notification.'}
    if body.get("boot_diagnostics"):
        return {"ok":True,"boot":db.kv_get("computer_boot_report"),"heartbeat":db.kv_get("computer_heartbeat")}
    if body.get("wake_fixture"):
        import cr_computer, cr_wake, os, time
        started=time.monotonic()
        if body["wake_fixture"]=="calculate":
            import re
            key=body.get("idempotency_key", "")
            if not isinstance(key,str) or not re.fullmatch(r"[A-Za-z0-9_-]{8,80}",key):
                return {"ok":False,"error":"A unique idempotency_key is required for the start fixture"}
            claim="wake_fixture:"+key
            row=db.q("INSERT INTO kv(key,value) VALUES(%s,%s::jsonb) ON CONFLICT(key) DO NOTHING RETURNING key",(claim,'{"state":"running"}'),"one")
            if not row:
                stored=db.kv_get(claim) or {}
                return {"ok":True,"duplicate":True,"fixture_status":stored}
            try:
                result=cr_computer.execute(cr_computer.OWNER,"calculate",{"expression":"20*(3+2)/4"},test_wake=True)
            except Exception as e:
                db.kv_set(claim,{"state":"failed","reason":type(e).__name__})
                raise
            db.kv_set(claim,{"state":"done","result":result})
        elif body["wake_fixture"]=="idle_stop":
            cr_wake.idle_stop(test=True);result={"last_activity":db.kv_get("computer_last_activity",0)}
        else:return {"ok":False,"error":"Unsupported wake fixture"}
        return {"ok":True,"wake_fixture":result,"elapsed_seconds":round(time.monotonic()-started,1)}
    if body.get("diagnostics"):
        users=db.q("SELECT user_id,name,last_seen FROM users WHERE user_id>0 ORDER BY last_seen DESC LIMIT 5")
        events=db.q("SELECT user_id,ts,event,detail FROM audit WHERE event IN ('telegram_reaction','media_processed','computer_lifecycle') ORDER BY ts DESC LIMIT 15")
        import threading,os
        return {"ok":True,"users":users,"events":events,"work_thread_alive":any(t.name=="internal-work-queue" and t.is_alive() for t in threading.enumerate()),"wake_env_ready":__import__("cr_wake").configured(),"lifecycle_token_present":bool(os.environ.get("CRAYON_GITHUB_LIFECYCLE_TOKEN")),"computer_heartbeat_age_seconds":max(0,round(__import__("time").time()-(db.kv_get("computer_heartbeat") or {}).get("at",0),1))}
    uid = int(body.get("uid", -4242))
    if uid >= 0:
        return {"ok": False, "error": "selftest only accepts negative synthetic users"}
    texts = body["texts"] if "texts" in body else [body.get("text", "hello")]
    results = []
    if body.get("reset"):
        mem.delete_all(uid)
    for t in texts:
        out = tg.CaptureOut()
        upd = {"message": {"message_id": 1, "chat": {"id": uid}, "from": {"id": uid, "first_name": body.get("name", "SelfTest")}, "text": t}}
        tg.handle_update(upd, out)
        results.append({"in": t, "out": [m["text"] for m in out.sent], "meta": out.meta, "reactions": out.reactions})
    if body.get("media_fixture"):
        import cr_media, base64
        fixture = body["media_fixture"]
        data = base64.b64decode(fixture.get("base64", ""), validate=True)
        if len(data) > cr_media.MAX_BYTES:
            return {"ok": False, "error": "fixture too large"}
        results.append({"media": fixture["mime"], "out": cr_media.analyze(data, fixture["mime"], fixture.get("caption", ""))})
    if body.get("reaction_fixture"):
        from unittest.mock import patch
        out = tg.CaptureOut()
        for text in ("Please build this", "Lets gooo", "thank you", "lol haha", "yes", "/help"):
            upd = {"message":{"message_id":1,"chat":{"id":uid},"from":{"id":uid},"text":text}}
            with patch.object(tg.A, "respond", return_value=("Captured test reply", {})):
                tg.handle_update(upd,out)
        results.append({"reactions":out.reactions,"note":"real transport is captured; no Telegram API calls"})
    if body.get("draft_fixture"):
        import cr_tools
        results.append({"draft": cr_tools.draft_message({"uid":uid}, "Sam", "Ask to reschedule a meeting; new time not chosen yet")})
    if body.get("seed_reminder"):
        from datetime import datetime, timezone, timedelta
        mem.touch_user(uid)
        db.q("INSERT INTO reminders(user_id,chat_id,text,due_at) VALUES(%s,%s,%s,%s)",
            (uid,uid,"Synthetic reminder",datetime.now(timezone.utc)+timedelta(minutes=60)),"none")
    if body.get("proactive_tick"):
        import cr_proactive
        out = tg.CaptureOut()
        # Controlled synthetic clock lets digest windows be verified without touching a real user.
        from unittest.mock import patch
        from datetime import datetime
        import cr_tools
        clock = body.get("synthetic_hour")
        if clock is not None and 0 <= int(clock) <= 23:
            n = cr_tools.now_local(uid).replace(hour=int(clock))
            with patch.object(cr_tools, "now_local", return_value=n):
                sent = cr_proactive.tick(out, only_user=uid)
        else:
            sent = cr_proactive.tick(out, only_user=uid)
        results.append({"proactive_sent": sent, "out": [m["text"] for m in out.sent]})
    if body.get("tick"):
        import cr_sched
        out = tg.CaptureOut()
        ids = cr_sched.tick(out, only_user=uid)
        results.append({"tick_delivered": ids, "sent": [m["text"] for m in out.sent]})
    if body.get("probe") == "reminders":
        rows = db.q("SELECT id,text,status,attempts FROM reminders WHERE user_id=%s ORDER BY id", (uid,))
        results.append({"reminders": [dict(r) for r in rows]})
    if body.get("probe") == "facts":
        results.append({"facts": [dict(key=f["key"], value=f["value"]) for f in mem.facts(uid)]})
    if body.get("task_states"):
        import cr_tools,cr_dashboard
        rows=db.q("SELECT id FROM tasks WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
        if rows:
            ident=rows[0]['id'];states=[]
            for step,status in body['task_states']:
                states.append(cr_tools.update_step({'uid':uid},ident,int(step),status,'Synthetic status proof'))
            results.append({'task_states':states,'dashboard':cr_dashboard.render(uid)})
    if body.get("work_controls"):
        import cr_work
        cr_work.init();out=tg.CaptureOut()
        rows=db.q("SELECT id FROM work_jobs WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
        if rows:
            ident=rows[0]['id']
            for op in body['work_controls']:
                cr_work.handle(uid,uid,"/work "+op+" "+str(ident),out)
            foreign=cr_work.get(uid-1,ident)
            results.append({"work_controls":[m['text'] for m in out.sent],"cross_user_visible":bool(foreign)})
    if body.get("work_tick"):
        import cr_work,cr_tools
        from unittest.mock import patch
        cr_work.init();out=tg.CaptureOut()
        n=cr_tools.now_local(uid)
        if body.get("work_hour") is not None:n=n.replace(hour=max(0,min(23,int(body["work_hour"]))))
        with patch.object(cr_tools,"now_local",return_value=n):
            done=cr_work.tick(out,only_user=uid)
        results.append({"work_tick":done,"out":[m["text"] for m in out.sent]})
    if body.get("work_probe"):
        import cr_work
        cr_work.init()
        rows=db.q("SELECT * FROM work_jobs WHERE user_id=%s ORDER BY id",(uid,))
        results.append({"work_probe":[cr_work.view(r) for r in rows]})
    if body.get("work_probe"):
        rows=db.q("SELECT status,notified,delivery_state FROM work_jobs WHERE user_id=%s ORDER BY id",(uid,))
        results.append({"work_notification_states":rows})
    if body.get("work_export"):
        import cr_work
        rows=db.q("SELECT id FROM work_jobs WHERE user_id=%s ORDER BY id DESC LIMIT 1",(uid,))
        out=tg.CaptureOut()
        if rows:cr_work.handle(uid,uid,"/work export "+str(rows[0]['id']),out)
        results.append({"work_export":[m["text"] for m in out.sent]})
    if body.get("cleanup"):
        mem.delete_all(uid)
    return {"ok": True, "results": results}
