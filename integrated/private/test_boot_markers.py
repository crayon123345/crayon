import os, pathlib, subprocess
import cr_selftest as S
import cr_computer as K

def test_script_marks_early_import_failure_without_secrets(tmp_path):
    script=tmp_path/'computer_start.sh'
    script.write_text(pathlib.Path('computer_start.sh').read_text())
    env={**os.environ,'HOME':str(tmp_path),'CRAYON_BRIDGE_TOKEN':'synthetic-do-not-print','CRAYON_COMPUTER_AUTOSTART':'on'}
    result=subprocess.run(['sh',str(script)],env=env)
    log=(tmp_path/'.config/crayon/boot-supervisor.log').read_text()
    assert result.returncode != 0
    assert 'script_entered' in log and 'supervisor_import_attempt' in log and 'script_exit' in log
    assert 'synthetic-do-not-print' not in log
    assert 'env_bridge_length=22' in log

def test_fixture_requires_idempotency_key(monkeypatch):
    monkeypatch.setattr(K,'execute',lambda *a,**kw: (_ for _ in ()).throw(AssertionError('must not start')))
    assert not S.run({'wake_fixture':'calculate'})['ok']

def test_fixture_atomic_claim_prevents_duplicate(monkeypatch):
    claims=set();saved={};calls=[]
    def q(sql,args,fetch):
        key=args[0]
        if key in claims:return None
        claims.add(key);return {'key':key}
    monkeypatch.setattr(S.db,'q',q)
    monkeypatch.setattr(S.db,'kv_set',lambda k,v:saved.update({k:v}))
    monkeypatch.setattr(S.db,'kv_get',lambda k:saved.get(k))
    monkeypatch.setattr(K,'execute',lambda *a,**kw: calls.append(1) or {'ok':True,'value':25})
    assert S.run({'wake_fixture':'calculate','idempotency_key':'marker-proof-001'})['ok']
    assert S.run({'wake_fixture':'calculate','idempotency_key':'marker-proof-001'})['duplicate']
    assert len(calls)==1
