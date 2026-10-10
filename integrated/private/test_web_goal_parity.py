import types,sys
import pytest
import cr_web_app as W

def prep(monkeypatch,destination=None):
 import cr_memory as M,cr_accounts,cr_agent
 monkeypatch.setattr(W.db,'q',lambda *a,**k:{'user_id':17})
 monkeypatch.setattr(M,'touch_user',lambda *a:None)
 monkeypatch.setattr(cr_accounts,'telegram_destination',lambda uid:destination)
 for name in ['cr_natural','cr_followups','cr_plugins','cr_persona','cr_voice','cr_games']:
  monkeypatch.setitem(sys.modules,name,types.SimpleNamespace(handle=lambda *a:False,translate=lambda t:t))
 return cr_agent

def test_goal_existing_canonical_engine(monkeypatch):
 a=prep(monkeypatch);calls=[]
 def respond(*args,**kw):calls.append((args,kw));return 'Plan saved',{'plan':['one','two'],'artifacts':[]}
 monkeypatch.setattr(a,'respond',respond)
 assert W.dispatch(17,'Name','/goal compare phones')[0]['text']=='Plan saved'
 assert calls==[((17,17,'compare phones','Name'),{'channel_name':'web','goal_mode':True})]

def test_goal_empty_and_private_account_guard(monkeypatch):
 a=prep(monkeypatch);monkeypatch.setattr(a,'respond',lambda *a,**kw:pytest.fail('no model/private effects'))
 assert 'Use /goal' in W.dispatch(17,'Name','/goal')[0]['text']
 assert 'No external action' in W.dispatch(17,'Name','/goal send email to X')[0]['text'] or 'No email sent' in W.dispatch(17,'Name','/goal send email to X')[0]['text']

def test_goal_does_not_consume_confirmations(monkeypatch):
 a=prep(monkeypatch);calls=[];monkeypatch.setattr(a,'respond',lambda *a,**kw:(calls.append(kw) or 'Answer',{}))
 W.dispatch(17,'Name','ordinary question');assert calls==[{'channel_name':'web','goal_mode':False}]
 W.dispatch(17,'Name','yes');assert len(calls)==1

def test_followup_uses_verified_linked_destination(monkeypatch):
 prep(monkeypatch,1898030949);calls=[]
 sys.modules['cr_followups'].handle=lambda *args:(calls.append(args[:3]) or True)
 assert W.dispatch(17,'Name','/followup 1h study')==[]
 assert calls==[(17,1898030949,'/followup 1h study')]
