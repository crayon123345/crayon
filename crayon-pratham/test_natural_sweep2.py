from unittest.mock import patch
import cr_work as W,cr_followups as F
class Out:
 def __init__(self):self.sent=[]
 def send(self,chat,text,**kw):self.sent.append(text)
def test_named_work_selection_no_numeric_homework():
 rows=[{'id':12,'title':'Solar panel brief','steps':[{'input':'solar panels'}]}]
 out=Out()
 with patch.object(W.db,'q',return_value=rows),patch.object(W,'init'),patch.object(W,'control',return_value={'id':12}),patch.object(W,'view',return_value='paused'),patch.object(W,'controls_markup',return_value={}) as controls:
  W.handle(1,1,'pause my research about solar panels',out)
  assert out.sent==['paused'];controls.assert_called_once()
def test_ambiguous_work_asks_names():
 out=Out()
 with patch.object(W.db,'q',return_value=[{'id':1,'title':'solar one','steps':[]},{'id':2,'title':'solar two','steps':[]}]),patch.object(W,'control') as control:
  W.handle(1,1,'pause work about solar',out)
  control.assert_not_called();assert 'solar one' in out.sent[0] and 'solar two' in out.sent[0]
def test_natural_followup_bounded():
 out=Out()
 with patch.object(F.T,'set_reminder',return_value={'verified':True}) as reminder,patch('cr_proactive.settings',return_value={'quiet_start':0,'quiet_end':0}):
  assert F.handle(1,1,'check in with me after 2 days about my presentation',out)
  assert reminder.call_args.kwargs['in_minutes']==2880

def test_web_draft_continuation_keeps_request_and_recipient():
 import cr_web_actions as X,cr_google_chat as H
 store={};seen=[]
 def classify(text):
  seen.append(text)
  return {'to':['sam@example.com'] if 'sam@example.com' in text else [],'cc':[],'bcc':[],'subject':'Domain','body':'Please buy a domain.'}
 with patch.object(X.db,'kv_get',side_effect=lambda k,d=None:store.get(k,d)),patch.object(X.db,'kv_set',side_effect=lambda k,v:store.update({k:v})),patch.object(X.G,'encrypt',side_effect=lambda uid,p:p),patch.object(X.G,'decrypt',side_effect=lambda uid,p:p),patch.object(H,'classify',side_effect=classify),patch.object(X,'preview',return_value={'review_id':'r'}):
  first=X.compose_preview(1,'Bearer token',{'message':'Write a mail requesting a domain'})
  assert first['kind']=='clarification'
  assert X.compose_preview(1,'Bearer token',{'message':'sam@example.com'})=={'review_id':'r'}
  assert 'requesting a domain' in seen[-1]
  other=X.compose_preview(2,'Bearer token',{'message':'sam@example.com'})
  assert 'requesting a domain' not in seen[-1]

def test_private_read_uses_returned_email_selection_no_id_homework():
 import cr_web_actions as X
 with patch.object(X.db,'kv_get',return_value={'ids':['a','b'],'until':9999999999}):
  assert X.natural_read_fields(1,'email_read','read the second email')=={'id':'b'}
  assert X.natural_read_fields(1,'email_read','read email 1')=={'id':'a'}
def test_private_file_link_extracts_id_without_model():
 import cr_web_actions as X
 assert X.natural_read_fields(1,'doc','read https://docs.google.com/document/d/abcdefghijklmnop/edit')=={'id':'abcdefghijklmnop'}

def test_private_read_selection_expired_or_other_session():
 import cr_web_actions as X,pytest
 with patch.object(X.db,'kv_get',return_value={'ids':['a'],'until':0}):
  with pytest.raises(ValueError):X.natural_read_fields(1,'email_read','read first email','s')
 with patch.object(X.db,'kv_get',return_value={}) as kv:
  with pytest.raises(ValueError):X.natural_read_fields(1,'email_read','read first email','other')
  assert kv.call_args.args[0]=='web_mail_results_1_other'

def test_plain_task_step_control_without_numbers():
 import cr_dashboard as D
 task={'id':7,'title':'Presentation','steps':[{'n':1,'title':'Draft outline','status':'done'},{'n':2,'title':'Practice talk','status':'todo'}]}
 with patch.object(D.T,'list_tasks',return_value={'tasks':[task]}):
  assert D.natural_control(1,'mark next step in Presentation done')=='/tasks done 7 2 | You reported this step done.'
  assert D.natural_control(1,'mark Practice step in Presentation blocked, waiting for slides')=='/tasks blocked 7 2 | waiting for slides'
  assert D.natural_control(1,'show my task Presentation')=='/tasks show 7'

def test_preview_calendar_and_sheet_return_clear_missing_question():
 import cr_web_actions as X,cr_natural as N
 with patch.object(X,'init'),patch.object(N,'calendar_fields',side_effect=N.Clarification('What day?')):
  assert X.preview(1,'Bearer s',{'kind':'calendar','fields':{'request':'create a meeting'}})=={'kind':'clarification','action':'calendar','text':'What day?'}
 with patch.object(X,'init'):
  assert X.preview(1,'Bearer s',{'kind':'sheet','fields':{'request':'update my budget sheet'}})['action']=='sheet'
