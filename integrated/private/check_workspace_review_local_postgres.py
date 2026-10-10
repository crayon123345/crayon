"""Disposable database only, providers mocked. No real Sheet write."""
import os
assert 'localhost:55432/crayon_test' in os.environ.get('DATABASE_URL','')
from cryptography.fernet import Fernet
os.environ['GOOGLE_TOKEN_ENCRYPTION_KEY']=Fernet.generate_key().decode()
import cr_workspace_review as R,cr_connections as X
from unittest.mock import patch
writes=[]
with patch.object(R.S,'sheet_preview',lambda uid,*args:{'payload':{'before':[[1]]},'hash':'h'}),patch.object(X,'status',lambda uid,p:{'identity':str(uid)+'@example.test'}),patch.object(R.S,'sheet_apply',lambda uid,*args:writes.append(uid) or {'verified':True}):
    d=R.preview(12,12,'sheet12345678900','A1',[[2]])
    try:R.confirm(13,13,d['id'],d['hash'],'confirm')
    except ValueError:pass
    else:raise AssertionError('wrong user')
    assert not writes
    assert 'true' in R.confirm(12,12,d['id'],d['hash'],'confirm')
    try:R.confirm(12,12,d['id'],d['hash'],'confirm')
    except ValueError:pass
    else:raise AssertionError('replay')
    assert writes==[12]
print('Postgres exact Sheet review rejects other user and replay, one approved mocked write')
R.db.q('DELETE FROM workspace_reviews WHERE user_id IN (12,13)',fetch='none')
