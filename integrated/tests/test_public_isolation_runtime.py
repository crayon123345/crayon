import os,sys,pathlib,asyncio,importlib
from starlette.testclient import TestClient
ROOT=pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'public'))
os.environ['CRAYON_INTEGRATED']='on'
os.environ['SECRET_KEY']='synthetic-unit-test-only'
os.environ['LOCAL_DB']='/tmp/crayon-integration-unit.sqlite'
import app as A
class FakeModel:
 async def astream(self, messages):
  class Chunk:content='Isolated synthetic reply'
  yield Chunk()
def test_identical_session_id_no_cross_visitor(monkeypatch):
 ids=iter([('v_'+'a'*32,False),('v_'+'b'*32,False)])
 monkeypatch.setattr(A.memory,'identity',lambda req:next(ids))
 monkeypatch.setattr(A.memory,'load_context',lambda vid:[('user',vid)])
 monkeypatch.setattr(A.memory,'save_turn',lambda *a:None)
 monkeypatch.setattr(A.memory,'save_chat_turn',lambda *a:None)
 monkeypatch.setattr(A.memory,'event',lambda *a:None)
 monkeypatch.setattr(A.llm,'plan',lambda text:[('gemini',None,'fixture')])
 monkeypatch.setattr(A,'model',lambda *a:FakeModel())
 A.sessions.clear()
 with TestClient(A.app) as c:
  r=c.post('/api/chat',json={'session_id':'same','message':'Synthetic1'});assert r.status_code==200
  r=c.post('/api/chat',json={'session_id':'same','message':'Synthetic2'});assert r.status_code==200
 assert len(A.sessions)==2
 assert A.sessions[('v_'+'a'*32,'same')][0]!=A.sessions[('v_'+'b'*32,'same')][0]
def test_personal_public_router_unavailable():
 with TestClient(A.app) as c:
  assert c.post('/api/mail/tool',json={'action':'send','args':{}}).status_code==404
  assert c.post('/telegram/webhook',json={}).status_code==404

def test_latest_raw_adapters_remain_disabled():
 with TestClient(A.app) as c:
  assert c.post('/api/tts',json={'text':'Synthetic'}).status_code==404
  assert c.post('/api/stt',content=b'Synthetic').status_code==404
  assert c.post('/whatsapp/d360',json={}).status_code==404
  assert c.post('/setup/d360-webhook',json={}).status_code==404

def test_integrated_image_generation_parked_before_identity_or_provider(monkeypatch):
 monkeypatch.setattr(A.memory,'identity',lambda req:(_ for _ in ()).throw(AssertionError('no identity or provider processing')))
 with TestClient(A.app) as c:
  r=c.get('/api/image?prompt=synthetic')
 assert r.status_code==503 and 'parked' in r.json()['error']

def test_image_completion_waits_for_load_and_studio_has_no_request():
 source=(ROOT/'public/static/app.js').read_text()
 block=source[source.index("const ig = acc.match"):source.index("const m = acc.match(/```python-run")]
 assert 'const imageReady = await loaded' in block
 assert block.index('im2.onload =')<block.index('im2.src = url;')<block.index('const imageReady = await loaded')
 assert 'Generation is not confirmed' in block
 assert block.index("location.pathname === '/studio'")<block.index('const url =')
 assert '[The image was generated and shown in the chat.]' not in block
