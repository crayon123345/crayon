import pytest
import cr_computer as K
import os
os.environ.setdefault('CRAYON_BRIDGE_URL','https://example.com')
os.environ.setdefault('CRAYON_BRIDGE_TOKEN','test-only')
import computer_worker as W

def test_all_valid_accounts_permitted():
    assert K.permitted(12) and K.permitted(10**15)
    for uid in (0,-1,True,'12',None,2**63):assert not K.permitted(uid)


def test_path_blocked():
    for x in ('../secret','.env','folder/x','/tmp/x'):
        with pytest.raises(ValueError):K.validate('read_text',{'filename':x})

def test_arithmetic_no_code():
    assert W.arithmetic('20 * (3+2) / 4')==25
    for x in ('__import__("os")','2 ** 1000000','open("x")','[1]*1000000'):
        with pytest.raises(ValueError):W.arithmetic(x)

def test_file_no_overwrite(tmp_path,monkeypatch):
    monkeypatch.setattr(W,'ROOT',tmp_path)
    assert W.run('write_text',{'filename':'proof.txt','text':'hello'},K.OWNER)['verified']
    assert W.run('read_text',{'filename':'proof.txt'},K.OWNER)['text']=='hello'
    with pytest.raises(FileExistsError):W.run('write_text',{'filename':'proof.txt','text':'bad'},K.OWNER)

def test_browser_public_safety():
    from computer_browser import allowed
    for url in ('http://example.com','https://127.0.0.1','https://accounts.google.com','https://paypal.com','https://docs.python.org:1234','https://example.com@evil.com'):
        with pytest.raises(ValueError):allowed(url)


def test_queue_retention(monkeypatch):
    queries=[]
    monkeypatch.setattr(K.db,'q',lambda sql,args,mode:queries.append(sql))
    monkeypatch.setattr(K.db,'kv_set',lambda *a:None)
    K.next_job({'system':'Linux'})
    assert queries[0].startswith('DELETE FROM computer_jobs')
    assert "30 minutes" in queries[0]


def test_private_file_isolation(tmp_path,monkeypatch):
    monkeypatch.setattr(W,'ROOT',tmp_path)
    for uid,body in ((K.OWNER,'owner secret'),(12,'member12'),(13,'member13')):
        W.run('write_text',{'filename':'same.txt','text':body},uid)
    for uid,body in ((K.OWNER,'owner secret'),(12,'member12'),(13,'member13')):
        assert W.run('read_text',{'filename':'same.txt','user_id':K.OWNER},uid)['text']==body
        assert W.run('list_files',{},uid)['files']==['same.txt']
    for uid in (None,-1,True):
        with pytest.raises(ValueError):W.run('read_text',{'filename':'same.txt'},uid)

def test_old_worker_cannot_receive_file_jobs(monkeypatch):
    monkeypatch.setattr(K,'status',lambda uid:{'ok':True})
    monkeypatch.setattr(K.db,'kv_get',lambda *a:{'info':{}})
    monkeypatch.setattr(K,'reserve',lambda uid:pytest.fail('No job should be reserved'))
    assert not K.execute(12,'list_files',{})['ok']

def test_tester_status_hides_machine(monkeypatch):
    monkeypatch.setattr(K.db,'kv_get',lambda *a:{'at':K.time.time(),'info':{'private':'secret'}})
    assert K.status(7555366869)['computer']=={}
    assert K.status(K.OWNER)['computer']=={'private':'secret'}

def test_execution_cap_failclosed(monkeypatch):
    monkeypatch.setattr(K,'status',lambda uid:{'ok':True})
    monkeypatch.setattr(K,'reserve',lambda uid:False)
    assert not K.execute(7555366869,'calculate',{'expression':'2+2'})['ok']


def test_public_social_and_login_walls_allowed(monkeypatch):
    import computer_browser as B
    monkeypatch.setattr(B.socket,'getaddrinfo',lambda *a:[(0,0,0,'',('1.1.1.1',443))])
    for url in ('https://www.instagram.com/accounts/login/','https://www.youtube.com','https://docs.github.com/login'):
        assert B.allowed(url)==url
    for url in ('https://mail.google.com','https://chase.com','https://x.com/cart/add','https://example.com/patient'):
        with pytest.raises(ValueError):B.allowed(url)


def test_old_worker_file_claim_blocked(monkeypatch):
    calls=[]
    monkeypatch.setattr(K.db,'q',lambda sql,args,mode:calls.append((sql,args)))
    monkeypatch.setattr(K.db,'kv_set',lambda *a:None)
    K.next_job({})
    assert calls[-1][1]==(False,False) and "operation NOT IN ('read_text','write_text','list_files')" in calls[-1][0]


def test_checkout_view_only_and_no_cart_mutations(monkeypatch):
    import computer_browser as B
    monkeypatch.setattr(B.socket,'getaddrinfo',lambda *a:[(0,0,0,'',('1.1.1.1',443))])
    assert B.allowed('https://example.com/checkout')
    assert B.allowed('https://example.com/cart')
    for url in ('https://example.com/cart/add?id=1','https://example.com/cart/clear','https://example.com/purchase/'):
        with pytest.raises(ValueError):B.allowed(url)

def test_browser_followup_gate_accepts_page_request(monkeypatch):
    import cr_tools as T
    monkeypatch.setattr(K,'execute',lambda *a:{'ok':False,'error':'test execution reached'})
    r=T.computer_browse({'uid':12,'meta':{'user_text':'Can you open the Are You With Me T shirt details page and show me'}},'https://example.com')
    assert r['error']=='test execution reached'

def test_multistep_plan_boundaries(monkeypatch):
    import computer_browser as B
    monkeypatch.setattr(B.socket,'getaddrinfo',lambda *a:[(0,0,0,'',('1.1.1.1',443))])
    B.validate_plan({'url':'https://example.com','follow_links':['Catalog','Product','Cart']})
    for steps in ('Catalog',['x']*6,[True]):
        with pytest.raises(ValueError):B.validate_plan({'url':'https://example.com','follow_links':steps})

def test_verified_heartbeat_clears_starting(monkeypatch):
    seen=[]
    monkeypatch.setattr(K.db,'q',lambda *a:None)
    monkeypatch.setattr(K.db,'kv_get',lambda key:'starting')
    monkeypatch.setattr(K.db,'kv_set',lambda *a:seen.append(a))
    K.next_job({'verified':True,'navigation_protocol':3})
    assert ('computer_lifecycle_state','ready') in seen
