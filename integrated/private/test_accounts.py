import pytest
import cr_accounts as K

class Tx:
    def __enter__(self):pass
    def __exit__(self,*a):pass
class Conn:
    def transaction(self):return Tx()

def test_standalone_range_not_group_or_phone():
    assert K.standalone(10**15) and not K.standalone(1898030949)
    assert not K.standalone(919692445876)

def test_standalone_no_invented_telegram_destination(monkeypatch):
    monkeypatch.setattr(K.db,'q',lambda *a:None)
    assert K.telegram_destination(10**15) is None
    assert K.telegram_destination(17)==17

def test_verified_google_legacy_storage_untouched(monkeypatch):
    calls=[]
    monkeypatch.setattr(K.db,'_conn',lambda:Conn())
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        if sql.startswith('SELECT g.user_id'):return {'user_id':17,'name':'N'}
    monkeypatch.setattr(K.db,'q',q)
    assert K.google_account({'subject':'verified-sub','email':'x@x','google_name':'New'})['user_id']==17
    assert not any(sql.startswith('INSERT INTO users') or sql.startswith('UPDATE') for sql,p in calls)

def test_new_google_never_email_matches(monkeypatch):
    calls=[]
    monkeypatch.setattr(K.db,'_conn',lambda:Conn())
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        if 'nextval' in sql:return {'id':10**15}
    monkeypatch.setattr(K.db,'q',q)
    assert K.google_account({'subject':'verified-sub','email':'existing@x','google_name':'N'})['user_id']==10**15
    assert not any('WHERE' in sql and 'email' in sql for sql,p in calls)
    assert not any(sql.startswith('DELETE') or sql.startswith('UPDATE') for sql,p in calls)

def test_link_existing_standalone_google_never_overwrites(monkeypatch):
    calls=[];monkeypatch.setattr(K.db,'_conn',lambda:Conn())
    def q(sql,p=(),fetch='all'):
        calls.append(sql)
        if sql.startswith('SELECT user_id'):return {'user_id':10**15}
    monkeypatch.setattr(K.db,'q',q)
    with pytest.raises(ValueError,match='reviewed merge'):K.link_legacy_google({'subject':'s','email':'x'},17,'N')
    assert not any(s.startswith('INSERT INTO web_google') for s in calls)

def test_first_login_rollback_is_transaction_scoped(monkeypatch):
    flags=[]
    class Transaction:
        def __enter__(self):flags.append('begin')
        def __exit__(self,*a):flags.append(a[0]);return False
    class Connection:
        def transaction(self):return Transaction()
    monkeypatch.setattr(K.db,'_conn',lambda:Connection())
    def q(sql,p=(),fetch='all'):
        if 'nextval' in sql:return {'id':10**15}
        if sql.startswith('INSERT INTO account_identities'):raise RuntimeError('database failure')
    monkeypatch.setattr(K.db,'q',q)
    with pytest.raises(RuntimeError):K.google_account({'subject':'s','email':'x','google_name':'N'})
    assert flags==['begin',RuntimeError]
