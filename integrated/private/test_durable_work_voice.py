from unittest.mock import patch
import cr_voice as V,cr_progress as P,cr_memory as M
class Out:
 def __init__(self):self.sent=[];self.files=[]
 def send(self,chat,text):self.sent.append(text)
 def artifact(self,chat,item):self.files.append(item)
def test_explicit_requested_greeting_audio():
 out=Out()
 with patch('cr_memory.add_message'),patch.object(V,'enabled',return_value=True),patch('cr_llm.generate',return_value={'text':'Hello! Good to hear from you.'}) as model,patch.object(V,'synthesize',return_value={'data':b'fixture','filename':'voice.mp3','mime':'audio/mpeg'}) as synth:
  assert V.handle(5,5,'Can you send me a voice note, greeting me?',out)
  assert out.files and synth.call_args.args[1]=='Hello! Good to hear from you.'
  assert 'text-only' in model.call_args.kwargs['system']
def test_ordinary_reply_never_generates_audio():
 out=Out()
 with patch.object(V,'synthesize') as synth:
  assert not V.handle(5,5,'Hello how are you?',out)
  synth.assert_not_called()
def test_optin_still_required():
 out=Out()
 with patch.object(V,'enabled',return_value=False),patch('cr_llm.generate') as model:
  assert V.handle(5,5,'Can you send me a voice note, greeting me?',out)
  model.assert_not_called();assert 'turn on voice' in out.sent[0]
def test_safe_work_metadata_saved_with_answer():
 token=P.recorded.set([])
 try:
  P.emit('Request accepted','done');P.emit('Preparing the response','done')
  with patch.object(M.db,'q') as q:
   M.add_message(5,'assistant','Hello')
   assert 'work_events' in q.call_args.args[0]
   assert 'Preparing the response' in q.call_args.args[1][-2]
 finally:P.recorded.reset(token)
