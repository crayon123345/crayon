import pytest
import cr_wake as W

def test_disabled_no_start(monkeypatch):
    monkeypatch.delenv('CRAYON_AUTO_WAKE',raising=False)
    monkeypatch.setattr(W,'call',lambda *a:pytest.fail('disabled start'))
    assert not W.ensure(lambda:False)
def test_ready_does_not_start(monkeypatch):
    monkeypatch.setattr(W,'touch',lambda:None)
    monkeypatch.setattr(W,'call',lambda *a:pytest.fail('already ready'))
    assert W.ensure(lambda:True)
def test_fixed_operation_and_no_redirect(monkeypatch):
    monkeypatch.setenv('CRAYON_AUTO_WAKE','on');monkeypatch.setenv('CRAYON_GITHUB_LIFECYCLE_TOKEN','synthetic')
    with pytest.raises(ValueError):W.call('delete')
    class Client:
        def __init__(self,**kw):assert not kw['follow_redirects']
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,url,**kw):
            assert url==W.API+'/start'
            return type('R',(),{'status_code':402})()
    monkeypatch.setattr(W.httpx,'Client',Client)
    with pytest.raises(ValueError,match='402'):W.call('start')
def test_idle_busy_no_stop(monkeypatch):
    monkeypatch.setattr(W,'configured',lambda:True)
    monkeypatch.setattr(W.db,'kv_get',lambda *a:1)
    monkeypatch.setattr(W.db,'q',lambda *a:{'id':'busy'})
    monkeypatch.setattr(W,'call',lambda *a:pytest.fail('busy stop'))
    W.idle_stop()

def test_idle_does_not_stop_busy(monkeypatch):
    monkeypatch.setattr(W,'configured',lambda:True)
    monkeypatch.setattr(W.db,'kv_get',lambda *a:1)
    monkeypatch.setattr(W.db,'q',lambda *a:{'id':'busy'})
    monkeypatch.setattr(W,'call',lambda *a:pytest.fail('busy stop'))
    W.idle_stop()


def test_accepted_stop_invalidates_heartbeat(monkeypatch):
    monkeypatch.setenv('CRAYON_AUTO_WAKE','on');monkeypatch.setenv('CRAYON_GITHUB_LIFECYCLE_TOKEN','synthetic')
    writes=[]
    class Client:
        def __init__(self,**kw):pass
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def post(self,*a,**kw):return type('R',(),{'status_code':200})()
    monkeypatch.setattr(W.httpx,'Client',Client)
    monkeypatch.setattr(W.db,'kv_set',lambda key,value:writes.append((key,value)))
    monkeypatch.setattr(W.db,'audit',lambda *a:None)
    assert W.call('stop')
    assert writes==[('computer_lifecycle_state','stopping'),('computer_heartbeat',{})]
    writes.clear();assert W.call('start')
    assert writes==[('computer_lifecycle_state','starting'),('computer_heartbeat',{})]


def test_stopping_host_is_not_ready_even_with_fresh_heartbeat(monkeypatch):
    import cr_computer as K
    state={'computer_heartbeat':{'at':K.time.time(),'info':{}},'computer_lifecycle_state':'stopping'}
    monkeypatch.setattr(K.db,'kv_get',lambda key,*a:state.get(key))
    assert not K.status(K.OWNER)['ok']
    state['computer_lifecycle_state']='starting'
    assert K.status(K.OWNER)['ok']
