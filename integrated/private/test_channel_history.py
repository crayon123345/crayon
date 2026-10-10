import pytest,json
from cryptography.fernet import Fernet
import cr_history as H
import cr_web_auth as A
class Out:
    def send(self,*a):return True

def test_command_receipt_generic_no_provider_data(monkeypatch):
    monkeypatch.setenv('CRAYON_WEB_ENCRYPTION_KEY',Fernet.generate_key().decode())
    calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p));return {'n':10} if sql.startswith('SELECT max') else None
    monkeypatch.setattr(H.db,'q',q)
    def handler(uid,chat,name,text,mid,out):out.send(chat,'PRIVATE MAIL BODY')
    H.run(17,17,'N','send it',5,Out(),handler)
    row=next(p for sql,p in calls if sql.startswith('INSERT'))
    data=json.loads(A._cipher().decrypt(row[2].encode()))
    assert 'PRIVATE MAIL BODY' not in str(data) and 'send it' not in str(data)
    assert row[1]==17

def test_ordinary_history_not_duplicated(monkeypatch):
    n=iter([10,12]);calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append(sql);return {'n':next(n)} if sql.startswith('SELECT max') else None
    monkeypatch.setattr(H.db,'q',q)
    H.run(17,17,'N','hello',5,Out(),lambda *a:None)
    assert not any(sql.startswith('INSERT') for sql in calls)

def test_group_and_secrets_not_recorded(monkeypatch):
    monkeypatch.setattr(H.db,'q',lambda *a,**k:pytest.fail('no record'))
    calls=[]
    for chat,text in [(-1,'read inbox'),(17,'password: veryprivate123')]:
        H.run(17,chat,'N',text,5,Out(),lambda *a:calls.append(a))
    assert len(calls)==2
