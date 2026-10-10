"""Regression: explicit all-account erasure removes content-free send receipts."""
import types,sys
import cr_memory as M

def test_send_decisions_in_all_account_erasure(monkeypatch):
    calls=[]
    def q(sql,p=(),fetch='all'):
        calls.append((sql,p))
        if sql.startswith('SELECT '):return {'n':0}
    monkeypatch.setattr(M.db,'q',q)
    for name in ['cr_google','cr_connections','cr_work','cr_web_rooms','cr_workspace_review','cr_whatsapp','cr_web_app','cr_account_merge']:
        module=types.SimpleNamespace(init=lambda:None,disconnect=lambda *args:None,delete_review_data=lambda *args:None,PROVIDERS=[])
        monkeypatch.setitem(sys.modules,name,module)
    assert M.delete_all(17)
    assert ('DELETE FROM web_send_decisions WHERE user_id=%s',(17,)) in calls
    assert not any('TRUNCATE' in sql or ('web_send_decisions' in sql and p!=(17,)) for sql,p in calls)
