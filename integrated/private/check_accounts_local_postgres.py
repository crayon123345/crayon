"""Run against a disposable local PostgreSQL DB only. Not production."""
import os
assert 'localhost:55432/crayon_test' in os.environ.get('DATABASE_URL','')
import cr_db as d,cr_accounts as K,cr_web_google_auth as G
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4
from cryptography.fernet import Fernet
os.environ['CRAYON_WEB_ENCRYPTION_KEY']=Fernet.generate_key().decode()
d.init();G.init();K.init();subject='local_'+uuid4().hex;i={'subject':subject,'email':'same@example.test','google_name':'Local'}
with ThreadPoolExecutor(max_workers=4) as p:ids=list(p.map(lambda _:K.google_account(i)['user_id'],range(8)))
assert len(set(ids))==1;assert K.telegram_destination(ids[0]) is None
try:K.link_legacy_google(i,17,'Local')
except ValueError:pass
else:raise AssertionError('must not overwrite')
print('8 concurrent first logins create exactly one standalone account; link cannot overwrite it')
import cr_web_email_auth as E,cr_web_auth as A
E.init();firebase_subject='local_'+uuid4().hex
email_identity={'subject':firebase_subject,'email':'same@example.test','name':'Email'}
email_account=K.identity_account('firebase_email',email_identity)
assert email_account['user_id']!=ids[0]
assert K.identity_account('firebase_email',email_identity)['user_id']==email_account['user_id']
# Local test fake provider read; no Firebase user, email or API mutation.
E.config=lambda:{};E.validate=lambda t:{'subject':'new_'+firebase_subject,'email':'link@example.test','name':''};E.admin_check=lambda t:{'auth_time':int(__import__('time').time())}
legacy_id=1000000+int(uuid4().hex[:7],16)
legacy={'user_id':legacy_id,'name':'Legacy'}
d.q('INSERT INTO users(user_id,name) VALUES(%s,%s)',(legacy_id,'Legacy'),'none')
review=E.link_preview(legacy,'Bearer '+'t'*43,{'id_token':'local-only'})
from firebase_admin import auth
E.admin_app=lambda:None
auth.get_user=lambda *a,**k:type('User',(),{'disabled':False,'email_verified':True,'email':'link@example.test','tokens_valid_after_timestamp':0})()
r=E.link_confirm(legacy,'Bearer '+'t'*43,{**{k:review[k] for k in ('review_id','hash')},'decision':'confirm'})
assert 'linked' in r['text']
try:E.link_confirm(legacy,'Bearer '+'t'*43,{**{k:review[k] for k in ('review_id','hash')},'decision':'confirm'})
except ValueError:pass
else:raise AssertionError('review replay')
print('Same-email Google/email remain isolated; exact email link attaches unused verified identity to fresh legacy account; replay rejected')
