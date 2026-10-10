"""Disposable local integration proof, never production."""
import os
assert 'localhost:55432/crayon_test' in os.environ.get('DATABASE_URL','')
from cryptography.fernet import Fernet
os.environ['CRAYON_WEB_ENCRYPTION_KEY']=Fernet.generate_key().decode()
import cr_db as d,cr_accounts as K,cr_account_merge as M
from uuid import uuid4
d.init();__import__('cr_web_google_auth').init();M.init();K.init()
for module in ('cr_web_app','cr_google','cr_connections','cr_calendar_draft','cr_booking','cr_work','cr_computer','cr_whatsapp'):
    __import__(module).init()
def pair():
    sub='merge_'+uuid4().hex
    source=K.identity_account('firebase_email',{'subject':sub,'email':'merge@example.test','name':'Source'})['user_id'];target=1000000+int(uuid4().hex[:7],16)
    d.q('INSERT INTO users(user_id,name,settings) VALUES(%s,%s,%s)',(target,'Telegram','{"proactive":false}'),'none')
    return source,target,sub
s,t,sub=pair()
for uid,value in [(s,'Source preference'),(t,'Telegram preference')]:d.q('INSERT INTO facts(user_id,key,value) VALUES(%s,%s,%s)',(uid,'preference',value),'none')
d.q('INSERT INTO messages(user_id,role,content) VALUES(%s,%s,%s)',(s,'user','Local source history'),'none')
a,h=M.snapshot(s,t)
try:M.apply_local_review(s,t,h,{})
except ValueError:pass
else:raise AssertionError('missing fact choice must fail')
assert d.q('SELECT user_id FROM users WHERE user_id=%s',(s,),'one')
M.apply_local_review(s,t,h,{'preference':'source'})
assert K.identity_account('firebase_email',{'subject':sub,'email':'merge@example.test','name':'Source'})['user_id']==t
assert d.q('SELECT value FROM facts WHERE user_id=%s',(t,),'one')['value']=='Source preference'
assert d.q('SELECT settings FROM users WHERE user_id=%s',(t,),'one')['settings']=={'proactive':False}
assert d.q('SELECT user_id FROM messages WHERE user_id=%s',(t,),'one')['user_id']==t
print('Atomic migration, fact choice, login rebinding, Telegram settings preservation passed locally')
s,t,_=pair();a,h=M.snapshot(s,t);d.q('INSERT INTO notes(user_id,text) VALUES(%s,%s)',(s,'Changed after review'),'none')
try:M.apply_local_review(s,t,h,{})
except ValueError as e:assert 'changed' in str(e)
else:raise AssertionError('stale snapshot')
assert d.q('SELECT user_id FROM users WHERE user_id=%s',(s,),'one')
print('Changed-snapshot rejection and rollback passed locally')
s,t,_=pair();d.q('UPDATE users SET settings=%s WHERE user_id=%s',('{"proactive":true}',s),'none')
try:M.snapshot(s,t)
except ValueError as e:assert 'grants' in str(e)
else:raise AssertionError('grant transfer blocked')
print('Source background-grant/settings transfer blocked locally')
# Session-bound one-use exact review, tested only with local fabricated provider claims.
import cr_web_auth as A
A.init();s,t,_=pair();token='t'*43
session=A._new_session({'user_id':s,'name':'Source'});header='Bearer '+session['token']
review=M.prepare_verified_review({'user_id':s},header,'a'*43,t,'Verified test Telegram')
body={'review_id':review['review_id'],'hash':review['hash'],'decision':'confirm','choices':{}}
try:M.confirm_verified_review({'user_id':s},'Bearer '+'z'*43,body)
except ValueError:pass
else:raise AssertionError('wrong session')
assert d.q('SELECT user_id FROM users WHERE user_id=%s',(s,),'one')
M.confirm_verified_review({'user_id':s},header,body)
try:M.confirm_verified_review({'user_id':s},header,body)
except ValueError:pass
else:raise AssertionError('review replay')
assert not d.q('SELECT user_id FROM web_sessions WHERE user_id IN (%s,%s)',(s,t))
print('Original-session binding, one-use review and old-session revocation passed locally')
try:d.q('INSERT INTO messages(user_id,role,content) VALUES(%s,%s,%s)',(s,'assistant','Delayed stale background write'),'none')
except Exception:pass
else:raise AssertionError('retired-account write must fail closed')
try:d.q('INSERT INTO users(user_id,name) VALUES(%s,%s)',(s,'Stale touch'),'none')
except Exception:pass
else:raise AssertionError('must not recreate retired source')
print('Late source writes and source-shell recreation fail closed locally')
# A delayed target summary/fact write must not overwrite the user's reviewed choices.
import cr_memory as mem
u=mem.get_user(t);current_epoch=u['memory_epoch'];assert current_epoch>=1
assert not mem.set_fact(t,'late_fact','Old model result',expected_epoch=current_epoch-1)
assert mem.set_fact(t,'fresh_fact','New model result',expected_epoch=current_epoch)
print('Target memory epoch rejects pre-merge model fact results locally')
# Mid-migration failure must roll back every row and retain both accounts.
s,t,sub=pair();d.q('INSERT INTO messages(user_id,role,content) VALUES(%s,%s,%s)',(s,'user','Rollback sentinel'),'none');_,stamp=M.snapshot(s,t)
original_q=d.q
class InjectedFailure(Exception):pass
def fail_after_history(sql,p=(),fetch='all'):
    if sql.startswith('UPDATE account_identities'):raise InjectedFailure()
    return original_q(sql,p,fetch)
d.q=fail_after_history
try:M.apply_local_review(s,t,stamp,{})
except InjectedFailure:pass
else:raise AssertionError('injected failure')
finally:d.q=original_q
assert d.q('SELECT user_id FROM users WHERE user_id=%s',(s,),'one')
assert d.q('SELECT user_id FROM messages WHERE user_id=%s',(s,),'one')
assert not d.q('SELECT source_id FROM account_redirects WHERE source_id=%s',(s,),'one')
print('Injected mid-migration failure rolls back rows, identity and redirect atomically')
# Group audience grants must not widen automatically to merged source history.
s,t,_=pair();d.kv_set('group_audience_v1_'+str(t)+'_-12345',True)
try:M.snapshot(s,t)
except ValueError as e:assert 'audience' in str(e)
else:raise AssertionError('expanded group disclosure must block')
print('Existing group-audience grant cannot silently widen to merged history')
# Concurrent writes can only land before snapshot (invalidate review) or after commit
# (retired-ID trigger rejection). Never an unreviewed orphan message.
from concurrent.futures import ThreadPoolExecutor
import threading
s,t,_=pair();_,stamp=M.snapshot(s,t)
event=threading.Event();original_q=d.q

def block_in_merge(sql,p=(),fetch='all'):
    result=original_q(sql,p,fetch)
    if sql.startswith('UPDATE messages SET user_id='):
        event.set();__import__('time').sleep(.15)
    return result

def late_writer():
    event.wait(timeout=5)
    try:d.q('INSERT INTO messages(user_id,role,content) VALUES(%s,%s,%s)',(s,'user','Racing message'),'none')
    except Exception:return 'rejected'
    return 'wrote'
d.q=block_in_merge
try:
    with ThreadPoolExecutor(max_workers=1) as pool:
        job=pool.submit(late_writer);M.apply_local_review(s,t,stamp,{});assert job.result(timeout=5)=='rejected'
finally:d.q=original_q
assert not d.q('SELECT user_id FROM messages WHERE user_id=%s',(s,))
print('Concurrent old-account insert waits for atomic commit, then retired-ID guard rejects it')
# Deletion strips private snapshots and destination association, not guard.
M.delete_review_data(t)
assert d.q('SELECT target_id FROM account_redirects WHERE source_id=%s',(s,),'one')['target_id'] is None
print('Retired-ID privacy cleanup retains only stale-write guard')
