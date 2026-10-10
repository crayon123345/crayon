import pytest
import cr_account_merge as M,cr_web_http as H,cr_web_auth as A
from test_web_app import Handler

def test_merge_http_disabled_even_with_valid_session(monkeypatch):
    monkeypatch.delenv('CRAYON_ACCOUNT_MERGE_ENABLED',raising=False)
    monkeypatch.setattr(A,'session',lambda h:{'user_id':10**15,'name':'Source'})
    monkeypatch.setattr(M,'confirm_verified_review',lambda *a:pytest.fail('disabled merge must not mutate'))
    h=Handler('/web/account-merge/confirm',{'Origin':A.origin(),'Content-Type':'application/json','Authorization':'Bearer '+'t'*43})
    H.handle(h,'POST',b'{}')
    assert h.code==400 and b'not enabled' in h.wfile.getvalue()

def test_merge_target_must_be_verified_telegram_range():
    with pytest.raises(ValueError,match='verified Telegram'):M.snapshot(10**15,10**15+1)
    with pytest.raises(ValueError):M.snapshot(17,18)

def test_merge_exact_choices_not_implicit():
    with pytest.raises(ValueError,match='Explicit fact'):M.apply_local_review(10**15,17,'a'*64,{'key':'guess'})


def test_merge_cookie_bridge_one_use_and_scoped(monkeypatch):
    monkeypatch.setenv('CRAYON_ACCOUNT_MERGE_ENABLED','on')
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    import json
    calls=[]
    data={'uid':10**15,'nonce':'n','verifier':'v'}
    monkeypatch.setattr(A.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)) or {'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode()})
    url,cookie=A.start_merge('s'*43)
    assert 'scope=openid+profile' in url and cookie and "cookie_hash=''" in calls[0][0]
    assert calls[0][1][0]==A.digest(cookie)


def test_merge_cookie_bridge_disabled_before_db(monkeypatch):
    monkeypatch.delenv('CRAYON_ACCOUNT_MERGE_ENABLED',raising=False)
    monkeypatch.setattr(A.db,'q',lambda *a:pytest.fail('disabled'))
    with pytest.raises(A.AuthError):A.start_merge('s'*43)

def test_db_never_reconnects_inside_atomic_transaction(monkeypatch):
    import cr_db as db,psycopg
    class Conn:
        _num_transactions=1
        def execute(self,*a):raise psycopg.OperationalError('Connection lost')
    monkeypatch.setattr(db.C,'DATABASE_URL','local-test-only')
    monkeypatch.setattr(db._local,'c',Conn(),raising=False)
    monkeypatch.setattr(db,'_conn',lambda:db._local.c)
    with pytest.raises(psycopg.OperationalError):db.q('SELECT 1')
    assert db._local.c._num_transactions==1

def test_signed_telegram_merge_callback_never_creates_login_handoff(monkeypatch):
    import json
    from cryptography.fernet import Fernet
    monkeypatch.setenv('CRAYON_ACCOUNT_MERGE_ENABLED','on')
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    data={'state':'s'*43,'verifier':'v','nonce':'n','uid':10**15,'name':'Source','session':'h'*64}
    calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append(sql)
        if sql.startswith('DELETE FROM web_login_states'):return {'encrypted':A._cipher().encrypt(json.dumps(data).encode()).decode(),'challenge':'a'*43}
        if sql.startswith('SELECT user_id'):return {'user_id':10**15}
    monkeypatch.setattr(A.db,'q',q)
    class Response:
        def raise_for_status(self):pass
        def json(self):return {'id_token':'signed-test-token'}
    monkeypatch.setattr(A.httpx,'post',lambda *a,**k:Response())
    monkeypatch.setattr(A,'validate_id_token',lambda *a:(17,'Verified Telegram'))
    seen=[];monkeypatch.setattr(M,'prepare_verified_review',lambda *a,**kw:seen.append((a,kw)))
    assert A.callback('s'*43,'code','c'*43)=='review'
    assert seen[0][0][3:]==(17,'Verified Telegram') and seen[0][1]['session_hash']=='h'*64
    assert not any(s.startswith('INSERT INTO web_login_codes') for s in calls)

def test_review_cleanup_removes_snapshots_not_retired_id_guard(monkeypatch):
    calls=[];monkeypatch.setattr(M,'init',lambda:None)
    monkeypatch.setattr(M.db,'q',lambda sql,p=(),fetch='all':calls.append((sql,p)))
    M.delete_review_data(17)
    assert calls[0][1]==(17,17) and calls[0][0].startswith('DELETE FROM account_merge_reviews')
    assert calls[1][0].startswith('UPDATE account_redirects SET target_id=NULL')
    assert not any(sql.startswith('DELETE FROM account_redirects') for sql,p in calls)
