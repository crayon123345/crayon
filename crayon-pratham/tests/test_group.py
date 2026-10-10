import cr_group as G
import cr_telegram as T


def test_mention_exact():
    assert G.mentioned({'text':'hi @crayon_v1_bot explain gravity'})
    assert not G.mentioned({'text':'hi @crayon_v1_bot_fake'})
    assert not G.mentioned({'text':'hello'})


def test_untagged_group_ignored(monkeypatch):
    monkeypatch.setattr(T.db,'kv_set',lambda *a: (_ for _ in ()).throw(AssertionError('private state touched')))
    out=T.CaptureOut()
    T.handle_update({'message':{'chat':{'id':-991,'type':'group'},'from':{'id':22},'text':'hello'}},out)
    assert not out.sent


def test_tagged_group_no_private_state(monkeypatch):
    monkeypatch.setattr(T.db,'kv_set',lambda *a: (_ for _ in ()).throw(AssertionError('private state touched')))
    monkeypatch.setattr(G,'answer',lambda msg:'Group answer')
    out=T.CaptureOut()
    T.handle_update({'message':{'chat':{'id':-991,'type':'supergroup'},'from':{'id':22},'text':'@crayon_v1_bot hi'}},out)
    assert out.sent[0]['text']=='Group answer'


def test_group_tools_disabled(monkeypatch):
    def gen(contents,system,tools):
        assert tools is None
        assert 'no private memory' in system
        return {'text':'Hi'}
    monkeypatch.setattr(G.llm,'generate',gen)
    assert G.answer({'text':'@crayon_v1_bot explain gravity'})=='Hi'
