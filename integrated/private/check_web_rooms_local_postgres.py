"""Disposable room audience/isolation proof only. No messages to real people."""
import os
assert 'localhost:55432/crayon_test' in os.environ.get('DATABASE_URL','')
from cryptography.fernet import Fernet
os.environ['CRAYON_WEB_ENCRYPTION_KEY']=Fernet.generate_key().decode()
import cr_web_rooms as R,cr_db as d,cr_group as G
from unittest.mock import patch
from uuid import uuid4
ids=[9000000+int(uuid4().hex[:6],16) for _ in range(3)]
for uid in ids:d.q('INSERT INTO users(user_id,name) VALUES(%s,%s)',(uid,'Local'),'none')
try:
    room=R.create(ids[0],'Local shared test',True)
    for uid in ids[1:]:
        try:R.history(uid,room['id'])
        except ValueError:pass
        else:raise AssertionError('Nonmember could read')
    R.join(ids[1],room['invite'],True)
    with patch.object(G,'answer',lambda msg:'public answer'):
        R.say(ids[1],room['id'],'public question')
    assert len(R.history(ids[0],room['id']))==2
    R.leave(ids[1],room['id'])
    try:R.history(ids[1],room['id'])
    except ValueError:pass
    else:raise AssertionError('Departed member read')
    try:R.delete(ids[2],room['id'],True)
    except ValueError:pass
    else:raise AssertionError('Other owner deleted')
    R.delete(ids[0],room['id'],True)
    assert not d.q('SELECT id FROM web_room_messages WHERE room_id=%s',(room['id'],),'one')
    print('Shared-room create/join/leave, nonmember privacy, own audience, owner-only delete cascade passed')
finally:
    for uid in ids:d.q('DELETE FROM users WHERE user_id=%s',(uid,),'none')
