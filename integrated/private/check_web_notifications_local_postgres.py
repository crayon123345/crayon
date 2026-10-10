"""Disposable database proof only. No real scheduled/Telegram effects."""
import os
assert 'localhost:55432/crayon_test' in os.environ.get('DATABASE_URL','')
from cryptography.fernet import Fernet
os.environ['CRAYON_WEB_ENCRYPTION_KEY']=Fernet.generate_key().decode()
import cr_db as d,cr_web_notifications as N
from uuid import uuid4
ids=[8000000+int(uuid4().hex[:6],16) for _ in range(2)]
for uid in ids:d.q('INSERT INTO users(user_id,name) VALUES(%s,%s)',(uid,'Local'),'none')
try:
    a=N.publish(ids[0],'private A','same');b=N.publish(ids[1],'private B','same')
    assert a!=b and N.publish(ids[0],'private A','same')==a
    assert [v['text'] for v in N.list_for(ids[0])]==['private A']
    assert [v['text'] for v in N.list_for(ids[1])]==['private B']
    N.acknowledge(ids[1],a);assert not N.list_for(ids[0])[0]['seen']
    N.acknowledge(ids[0],a);assert N.list_for(ids[0])[0]['seen']
    print('Encrypted private notifications: account separation, dedup and cross-owner acknowledgement refusal')
finally:
    for uid in ids:
        d.q('DELETE FROM web_notifications WHERE user_id=%s',(uid,),'none')
        d.q('DELETE FROM users WHERE user_id=%s',(uid,),'none')
