import json
import computer_boot as B
import cr_computer as K

def test_scrub_masks_known_and_unknown_secrets(monkeypatch):
    monkeypatch.setenv('CRAYON_GITHUB_LIFECYCLE_TOKEN','short-exact-secret')
    text=B.scrub('bridge-value short-exact-secret github_pat_'+'x'*40+' https://host/path?key=bad','bridge-value')
    assert 'bridge-value' not in text and 'short-exact-secret' not in text and 'x'*40 not in text and 'https' not in text

def test_publish_private_local_even_if_network_unavailable(tmp_path,monkeypatch):
    monkeypatch.setattr(B,'STATE',tmp_path);monkeypatch.setenv('CRAYON_BRIDGE_URL','https://other.invalid')
    assert not B.publish({'state':'failed','log':'my-secret-value'},'my-secret-value')
    out=json.loads((tmp_path/'boot-report.json').read_text())
    assert out['log']=='[redacted]' and (tmp_path/'boot-report.json').stat().st_mode & 0o777 == 0o600

def test_admin_boot_storage_allowlist(monkeypatch):
    saved=[];monkeypatch.setattr(K.db,'kv_set',lambda k,v:saved.append((k,v)))
    assert K.record_boot({'state':'failed','log':'https://private.invalid/path','ignored':'bad'})=={'ok':True}
    assert saved[0][0]=='computer_boot_report' and 'ignored' not in saved[0][1]
    assert 'private.invalid' not in saved[0][1]['log']
