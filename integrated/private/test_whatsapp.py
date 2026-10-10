import hashlib
import hmac
import json

import cr_telegram as T
import cr_whatsapp as W

OWNER = "918777566396"


def env(monkeypatch):
    for k, v in {"WHATSAPP_ACCESS_TOKEN": "t", "WHATSAPP_PHONE_NUMBER_ID": "123", "WHATSAPP_APP_SECRET": "secret",
                 "WHATSAPP_VERIFY_TOKEN": "vt", "CRAYON_OWNER_WA_ID": OWNER}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr(W.db, 'available', lambda: True)
    rows = {}
    def query(sql, params=(), fetch='all'):
        if 'INSERT INTO whatsapp_inbox' in sql:
            mid,sender,kind,value,name=params
            if mid in rows: return None
            rows[mid]={'message_id':mid,'sender':sender,'kind':kind,'value':value,'profile_name':name,'state':'queued'}
            return {'message_id':mid}
        if "SET state='processing'" in sql:
            row=rows.get(params[0])
            if row and row['state']=='queued': row['state']='processing'; return row
        if "SET state='done'" in sql: rows[params[0]]['state']='done'
        return None
    monkeypatch.setattr(W.db, 'q', query)


def payload(sender, mid, text=None, button=None, typ=None):
    m = {"id": mid, "from": sender}
    if button:
        m.update(type="interactive", interactive={"button_reply": {"id": button}})
    elif typ:
        m["type"] = typ
    else:
        m.update(type="text", text={"body": text})
    return {"object": "whatsapp_business_account", "entry": [{"changes": [{"value": {
        "metadata": {"phone_number_id":"123"}, "contacts": [{"wa_id": sender, "profile": {"name": "Uttiya"}}], "messages": [m]}}]}]}


def sign(p, secret="secret"):
    raw = json.dumps(p).encode()
    return raw, "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


class Sync:
    def submit(self, fn, *a):
        fn(*a)


def test_handshake(monkeypatch):
    env(monkeypatch)
    assert W.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "vt", "hub.challenge": "42"}) == "42"
    assert W.verify_challenge({"hub.mode": "subscribe", "hub.verify_token": "bad", "hub.challenge": "42"}) is None


def test_bad_signature_and_disabled(monkeypatch):
    env(monkeypatch)
    monkeypatch.setattr(W, "_pool", Sync())
    monkeypatch.setattr(W, "process", lambda *a: (_ for _ in ()).throw(AssertionError("processed")))
    raw, _ = sign(payload(OWNER, "m1", "hi"))
    assert W.handle_webhook(raw, "sha256=00") == 403
    assert W.handle_webhook(raw, None) == 403
    monkeypatch.delenv("WHATSAPP_APP_SECRET")
    assert W.handle_webhook(raw, "sha256=00") == 404


def test_owner_only_and_dedupe(monkeypatch):
    env(monkeypatch)
    monkeypatch.setattr(W, "_pool", Sync())
    calls = []
    monkeypatch.setattr(W, "process", lambda kind, value, uid, name, out=None: calls.append((kind, value, uid, name)))
    for p in (payload("919000000000", "m0", "hi"), payload(OWNER, "m1", "hi"), payload(OWNER, "m1", "hi")):
        raw, sig = sign(p)
        assert W.handle_webhook(raw, sig) == 200
    assert calls == [("text", "hi", int(OWNER), "Uttiya")]


def test_text_becomes_private_update(monkeypatch):
    seen = {}
    monkeypatch.setattr(T, "handle_update", lambda upd, out=None: seen.update(upd=upd, out=out))
    W.process("text", "remind me", int(OWNER), "Uttiya")
    m = seen["upd"]["message"]
    assert m["chat"] == {"id": int(OWNER), "type": "private"} and m["from"]["id"] == int(OWNER) and m["text"] == "remind me"
    assert isinstance(seen["out"], W.WhatsAppOut)


def test_button_reply_becomes_callback(monkeypatch):
    seen = {}
    monkeypatch.setattr(T, "handle_callback", lambda cb, out: seen.update(cb=cb))
    W.process("button", "email_send:1:abc", int(OWNER), "")
    assert seen["cb"]["data"] == "email_send:1:abc" and seen["cb"]["from"]["id"] == int(OWNER) == seen["cb"]["message"]["chat"]["id"]


def test_non_text_gets_text_only_reply():
    class Cap:
        def __init__(self): self.sent = []
        def send(self, c, t, markup=None): self.sent.append(t)
    c = Cap()
    W.process("other", "", int(OWNER), "", c)
    assert "Text only" in c.sent[0]


def test_out_sends_buttons_and_list(monkeypatch):
    env(monkeypatch)
    posts = []
    monkeypatch.setattr(W, "_post", lambda body: posts.append(body) or "id")
    o = W.WhatsAppOut()
    o.send(OWNER, "Review", {"inline_keyboard": [[{"text": "Send", "callback_data": "email_send:1:a"}, {"text": "Cancel", "callback_data": "email_cancel:1"}]]})
    assert posts[0]["type"] == "text" and posts[1]["interactive"]["type"] == "button"
    assert [b["reply"]["id"] for b in posts[1]["interactive"]["action"]["buttons"]] == ["email_send:1:a", "email_cancel:1"]
    posts.clear()
    five = {"inline_keyboard": [[{"text": "B%d" % i, "callback_data": "work:show:%d" % i}] for i in range(1, 6)]}
    o.send(OWNER, "Job", five)
    assert posts[1]["interactive"]["type"] == "list" and len(posts[1]["interactive"]["action"]["sections"][0]["rows"]) == 5
    posts.clear()
    o.send(OWNER, "x" * 8000)
    assert len(posts) == 3 and all(p["type"] == "text" for p in posts)
    assert o.react(OWNER, 1, "👍") is False and o.delete(OWNER, 1) is False


def test_telegram_out_routes_whatsapp_chat_ids(monkeypatch):
    env(monkeypatch)
    sent = []
    monkeypatch.setattr(W, "_post", lambda body: sent.append(body) or "id")
    monkeypatch.setattr(T, "api", lambda *a, **k: (_ for _ in ()).throw(AssertionError("telegram used")))
    T.Out().send(int(OWNER), "Reminder: call Mum")
    assert sent[0]["to"] == OWNER and sent[0]["text"]["body"] == "Reminder: call Mum"
    assert W.owns(int(OWNER)) and not W.owns(12345) and not W.owns(None)


def status_payload(state='delivered', mid='out1', recipient=OWNER, timestamp='100', phone='123', code=141006):
    return {'object':'whatsapp_business_account','entry':[{'changes':[{'value':{
        'metadata':{'phone_number_id':phone},'statuses':[{'id':mid,'recipient_id':recipient,
        'status':state,'timestamp':timestamp,'errors':[{'code':code,'message':'DO NOT STORE PRIVATE PROSE'}]}]}}]}]}


def test_statuses_signed_filtered_and_correlated(monkeypatch):
    env(monkeypatch)
    calls=[]
    monkeypatch.setattr(W, 'record_status', lambda *args: calls.append(args))
    for p in [status_payload(), status_payload(phone='different'), status_payload(recipient='919000000000'),
              status_payload(state='unknown'),status_payload(timestamp='bad')]:
        raw,sig=sign(p)
        assert W.handle_webhook(raw,sig)==200
    assert calls==[('out1',OWNER,'delivered',100,'141006')]
    raw,_=sign(status_payload())
    assert W.handle_webhook(raw,'sha256=bad')==403
    assert len(calls)==1


def test_status_storage_monotonic_and_race_safe(monkeypatch):
    env(monkeypatch)
    calls=[]
    monkeypatch.setattr(W.db,'q', lambda sql,params=(),fetch='all':calls.append((sql,params)))
    W.record_status('out1',OWNER,'failed',100,'141006')
    sql,params=calls[0]
    assert 'ON CONFLICT(message_id)' in sql and 'status_ts<=EXCLUDED.status_ts' in sql
    assert "state='delivered' AND EXCLUDED.state='read'" in sql
    assert params==('out1',OWNER,'failed',100,'141006')
    W.delivery('out1')
    assert 'accepted=true' in calls[-1][0]


def test_webhook_persistence_failure_retriable_and_malformed(monkeypatch):
    env(monkeypatch)
    monkeypatch.setattr(W.db,'q',lambda *a,**k:(_ for _ in ()).throw(RuntimeError('db unavailable')))
    raw,sig=sign(payload(OWNER,'m1','hi'))
    assert W.handle_webhook(raw,sig)==503
    for p in ([], {'object':'whatsapp_business_account','entry':[None]}):
        raw,sig=sign(p)
        assert W.handle_webhook(raw,sig)==400


def test_wrong_phone_and_group_payload_never_private(monkeypatch):
    env(monkeypatch)
    monkeypatch.setattr(W,'enqueue',lambda *a:(_ for _ in ()).throw(AssertionError('enqueued')))
    for mode in ('phone','group','message_group'):
        p=payload(OWNER,'m1','private')
        v=p['entry'][0]['changes'][0]['value']
        if mode=='phone':v['metadata']['phone_number_id']='wrong'
        elif mode=='group':v['group_id']='g'
        else:v['messages'][0]['group_id']='g'
        raw,sig=sign(p)
        assert W.handle_webhook(raw,sig)==200


def test_send_no_fake_success_and_no_nonallowed_recipient(monkeypatch):
    env(monkeypatch)
    monkeypatch.setattr(W,'_post',lambda body:None)
    import pytest
    with pytest.raises(RuntimeError):W.WhatsAppOut().send(OWNER,'hi')
    with pytest.raises(ValueError):W.WhatsAppOut().send('919000000000','hi')
    with pytest.raises(ValueError):W.WhatsAppOut().artifact('919000000000',{})


def test_clean_text_and_secret_redaction(monkeypatch):
    env(monkeypatch)
    monkeypatch.setenv('WHATSAPP_ACCESS_TOKEN','test-private-secret-value')
    sent=[]
    monkeypatch.setattr(W,'_post',lambda body:sent.append(body) or 'out1')
    assert W.WhatsAppOut().send(OWNER,'**Hello** \u2014 "test-private-secret-value"')==['out1']
    assert sent[0]['text']['body']=='Hello  -  "[redacted]"'
    assert sent[0]['text']['preview_url'] is False


def test_post_requires_real_wamid_and_tracks_accepted(monkeypatch):
    env(monkeypatch)
    class Response:
        status_code=200
        def json(self):return {'messages':[{'id':'wamid.real'}]}
    monkeypatch.setattr(W._http,'post',lambda *a,**k:Response())
    calls=[]
    monkeypatch.setattr(W.db,'q',lambda sql,params=(),fetch='all': {'ok':1} if sql.startswith('SELECT 1') else calls.append((sql,params)))
    assert W._post({'to':OWNER,'type':'text'})=='wamid.real'
    assert calls[0][1]==('wamid.real',OWNER)
    assert 'accepted' in calls[0][0]
    monkeypatch.setattr(Response,'json',lambda self:{'success':True})
    assert W._post({'to':OWNER,'type':'text'}) is None


def test_failed_processing_never_replayed(monkeypatch):
    env(monkeypatch)
    calls=[]
    row={'kind':'text','value':'hi','sender':OWNER,'profile_name':'Owner'}
    def query(sql,params=(),fetch='all'):
        calls.append((sql,params))
        if "SET state='processing'" in sql:return row
    monkeypatch.setattr(W.db,'q',query)
    monkeypatch.setattr(W,'process',lambda *a:(_ for _ in ()).throw(RuntimeError('uncertain')))
    W._drain_one('m1')
    assert "state='queued'" in calls[0][0]
    assert "state='uncertain',value='',profile_name=''" in calls[-1][0]


def test_recovery_queued_only_retention(monkeypatch):
    env(monkeypatch)
    calls=[]
    def query(sql,params=(),fetch='all'):
        calls.append(sql)
        return [] if sql.startswith('SELECT') else None
    monkeypatch.setattr(W.db,'q',query)
    W.drain_pending()
    assert "state='uncertain'" in calls[0]
    assert "state='queued'" in calls[1] and 'LIMIT 25' in calls[1]
    assert all("interval '7 days'" in x for x in calls[2:])


def test_no_template_paid_route_or_outside_service_window(monkeypatch):
    env(monkeypatch)
    monkeypatch.setattr(W._http,'post',lambda *a,**k:(_ for _ in ()).throw(AssertionError('network')))
    assert W._post({'to':OWNER,'type':'template'}) is None
    assert W._post({'to':'919000000000','type':'text'}) is None
    monkeypatch.setattr(W.db,'q',lambda *a,**k:None)
    assert W._post({'to':OWNER,'type':'text'}) is None
    assert W.verify_signature('secret',b'{}',123) is False


def test_signature_exact_bytes_no_json_reserialization(monkeypatch):
    env(monkeypatch)
    raw,sig=sign(payload(OWNER,'m1','hi'))
    assert W.verify_signature('secret',raw,sig)
    assert not W.verify_signature('secret',raw+b' ',sig)
