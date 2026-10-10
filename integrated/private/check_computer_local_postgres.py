"""Disposable PostgreSQL isolation proof. Never production."""
import os,json,time
assert 'localhost:55432/crayon_test' in os.environ.get('DATABASE_URL','')
import cr_computer as K,cr_db as d
from unittest.mock import patch
K.init();d.q('DELETE FROM computer_jobs',fetch='none')
# Captured worker completion; no host, Telegram or provider action.
def worker_sleep(seconds):
    job=K.next_job({'account_files_protocol':2})
    if job:
        assert job['user_id']==12 and 'user_id' not in job['args']
        K.complete(job['id'],{'ok':True,'verified':True,'value':4})
with patch.object(K,'status',lambda uid:{'ok':True}),patch.object(K,'reserve',lambda uid:True),patch.object(K.time,'sleep',worker_sleep),patch('cr_wake.touch',lambda:None):
    r=K.execute(12,'calculate',{'expression':'2+2'})
assert r['value']==4
row=d.q('SELECT user_id FROM computer_jobs LIMIT 1',fetch='one');assert row['user_id']==12
print('Authenticated queue ownership survives bridge readback and completion')
import computer_worker as W
from tempfile import TemporaryDirectory
from pathlib import Path
with TemporaryDirectory() as folder,patch.object(W,'ROOT',Path(folder)):
    for uid,text in ((K.OWNER,'legacy'),(12,'twelve'),(13,'thirteen')):
        assert W.run('write_text',{'filename':'same.txt','text':text},uid)['verified']
    assert W.run('read_text',{'filename':'same.txt','user_id':K.OWNER},12)['text']=='twelve'
    assert W.run('read_text',{'filename':'same.txt'},13)['text']=='thirteen'
    assert W.run('read_text',{'filename':'same.txt'},K.OWNER)['text']=='legacy'
print('Three private file owners, same filename, forged args cannot switch owner')
