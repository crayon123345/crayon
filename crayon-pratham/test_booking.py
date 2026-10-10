import pytest,json,hashlib
import cr_booking as B
import computer_forms as F
import cr_computer as K

def config():return {'url':'https://example.com/form','action':'https://example.com/result','fields':['name','email'],'confirmation':'Received','terms':'Free, no cancellation fee, no external message. Reviewed test.'}
@pytest.fixture(autouse=True)
def no_dns(monkeypatch):monkeypatch.setattr(F,'allowed',lambda x:x)
def test_form_schema_exact_and_public():
    c=config();assert F.validate(c,{'name':'A','email':'a@example.com'})
    for change in ({'fields':['password']},{'action':'https://evil.com/result'},{'terms':''},{'extra':'anything'},{'fields':['name','name']}):
        with pytest.raises(ValueError):F.validate({**c,**change})
    for values in ({'name':'A'},{'name':'A','email':'x','password':'secret'}, {'name':'password=123456789','email':'x'}):
        with pytest.raises(ValueError):F.validate(c,values)
def test_form_fingerprint_changes():
    assert F.fingerprint({'name':'one'})!=F.fingerprint({'name':'two'})
def test_booking_all_accounts_and_invalid_identity():
    B.gate(12)
    for uid in (0,-1,True,'12'):
        with pytest.raises(B.BookingError):B.gate(uid)
def test_preview_does_not_submit(monkeypatch):
    calls=[];queries=[];c=config();values={'name':'Test','email':'test@example.com'}
    monkeypatch.setattr(B,'configs',lambda:{'test':c})
    monkeypatch.setattr(K,'execute',lambda u,op,args:calls.append(op) or {'ok':True,'verified':True,'hash':'a'*64})
    monkeypatch.setattr(B.db,'q',lambda sql,*a,**k:queries.append(sql))
    monkeypatch.setattr(B.db,'kv_set',lambda *a:None)
    monkeypatch.setattr(B.G,'encrypt',lambda u,c:'encrypted')
    d=B.preview(K.OWNER,'test',values)
    assert calls==['form_inspect'] and 'not submitted' in d['text'] and 'test@example.com' in d['text']
    assert any('10 minutes' in q for q in queries)
def test_stale_or_other_review_never_submits(monkeypatch):
    monkeypatch.setattr(B.db,'q',lambda *a,**k:None)
    monkeypatch.setattr(B.db,'kv_get',lambda *a:None)
    monkeypatch.setattr(K,'execute',lambda *a:pytest.fail('stale submit'))
    with pytest.raises(B.BookingError,match='Review'):B.submit(K.OWNER,'id','hash')
    monkeypatch.setattr(B.db,'kv_get',lambda *a:['id','hash'])
    with pytest.raises(B.BookingError,match='expired'):B.submit(K.OWNER,'id','hash')
def test_submit_claim_single_use_and_exact(monkeypatch):
    c=config();content={'config':c,'values':{'name':'Test','email':'test@example.com'},'page_hash':'a'*64};h=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest();claimed=[];calls=[]
    def q(sql,*a,**k):
        if 'RETURNING encrypted_content' in sql:
            if claimed:return None
            claimed.append(True);assert "status='pending'" in sql and 'expires_at>now()' in sql
            return {'encrypted_content':'x','content_hash':h}
    monkeypatch.setattr(B.db,'q',q);monkeypatch.setattr(B.db,'kv_get',lambda *a:['id',h[:12]])
    monkeypatch.setattr(B.G,'decrypt',lambda *a:content);monkeypatch.setattr(B,'configs',lambda:{'test':c})
    monkeypatch.setattr(K,'execute',lambda u,op,args:calls.append((op,args)) or {'ok':True,'verified':True,'note':'Received'})
    assert B.submit(K.OWNER,'id',h[:12])['verified']
    with pytest.raises(B.BookingError):B.submit(K.OWNER,'id',h[:12])
    assert len(calls)==1 and calls[0][0]=='form_submit' and calls[0][1]['expected_hash']=='a'*64
def test_configuration_change_no_submit(monkeypatch):
    content={'config':config(),'values':{'name':'Test','email':'t@example.com'},'page_hash':'a'*64};h=hashlib.sha256(json.dumps(content,sort_keys=True).encode()).hexdigest()
    monkeypatch.setattr(B.db,'q',lambda sql,*a,**k:{'encrypted_content':'x','content_hash':h} if 'RETURNING encrypted_content' in sql else None)
    monkeypatch.setattr(B.db,'kv_get',lambda *a:['id',h[:12]]);monkeypatch.setattr(B.G,'decrypt',lambda *a:content);monkeypatch.setattr(B,'configs',lambda:{})
    monkeypatch.setattr(K,'execute',lambda *a:pytest.fail('changed adapter submit'))
    with pytest.raises(B.BookingError,match='configuration changed'):B.submit(K.OWNER,'id',h[:12])
def test_member_form_payload_still_checked():
    for op in ('form_submit','form_inspect'):
        with pytest.raises(ValueError):K.execute(12,op,{})
def test_requires_current_page_hash():
    with pytest.raises(ValueError):K.validate('form_submit',{'config':config(),'values':{'name':'Test','email':'t@example.com'}})

@pytest.mark.skipif(not __import__('pathlib').Path('/usr/bin/google-chrome').exists(),reason='Local browser executable not installed')
def test_browser_form_end_to_end_and_changed_page(monkeypatch):
    import threading
    from http.server import HTTPServer,BaseHTTPRequestHandler
    BrowserType=pytest.importorskip("playwright.sync_api").BrowserType
    posts=[];changed=[False]
    class H(BaseHTTPRequestHandler):
        def log_message(self,*a):pass
        def do_GET(self):
            body=('<title>Test form</title><h1>'+('Changed' if changed[0] else 'Controlled test')+'</h1><form action="/result" method="post"><input name="name" required><input name="email" type="email" required><button type="submit">Submit</button></form>').encode();self.send_response(200);self.end_headers();self.wfile.write(body)
        def do_POST(self):
            posts.append(self.rfile.read(int(self.headers['Content-Length'])))
            self.send_response(200);self.end_headers();self.wfile.write(b'<h1>Received</h1>')
    server=HTTPServer(('127.0.0.1',0),H);threading.Thread(target=server.serve_forever,daemon=True).start()
    launch=BrowserType.launch
    monkeypatch.setattr(BrowserType,'launch',lambda self,**kw:launch(self,**{**kw,'executable_path':'/usr/bin/google-chrome','args':kw.get('args',[])+['--no-sandbox']}))
    c={**config(),'url':f'http://127.0.0.1:{server.server_port}/form','action':f'http://127.0.0.1:{server.server_port}/result'}
    try:
        r=F.run(c);assert r['verified'] and not posts
        changed[0]=True
        with pytest.raises(ValueError,match='changed'):F.run(c,{'name':'Test','email':'t@example.com'},r['hash'])
        assert not posts
        changed[0]=False
        result=F.run(c,{'name':'Test','email':'t@example.com'},r['hash'])
        assert result['verified'] and len(posts)==1 and b'name=Test' in posts[0]
        import pathlib
        pathlib.Path('/downloads/form-foundation-local-preview.png').write_bytes(__import__('base64').b64decode(r['screenshot']))
        pathlib.Path('/downloads/form-foundation-local-receipt.png').write_bytes(__import__('base64').b64decode(result['screenshot']))
    finally:server.shutdown()
