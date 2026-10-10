import pytest
from concurrent.futures import ThreadPoolExecutor
from cr_voice_state import VoiceState,VoiceError
@pytest.fixture
def state():
 now=[1000];active=[True];s=VoiceState('https://crayon.example',lambda u,x:active[0] and u==1 and x=='session',lambda:now[0]);return s,now,active
def issue(s):return s.issue(1,'session','conversation','https://crayon.example','live',True)
def redeem(s,t):return s.redeem(t['ticket'],'https://crayon.example','live','adapter')
def test_consent_origin(state):
 s,_,_=state
 for origin in [None,'null','','https://evil.example']:
  with pytest.raises(VoiceError):s.issue(1,'session','conversation',origin,'live',True)
 with pytest.raises(VoiceError):s.issue(1,'session','conversation',s.origin,'live',False)
def test_expiry_boundary(state):
 s,n,_=state;t=issue(s);n[0]+=120
 with pytest.raises(VoiceError,match='expired'):redeem(s,t)
def test_race_once(state):
 s,_,_=state;t=issue(s)
 def claim(_):
  try:return redeem(s,t)
  except VoiceError:return None
 with ThreadPoolExecutor(8) as pool:assert sum(bool(x) for x in pool.map(claim,range(8)))==1
def test_binding_and_rate(state):
 s,_,_=state;t=issue(s)
 with pytest.raises(VoiceError):s.redeem(t['ticket'],s.origin,'speak','adapter')
 for _ in range(5):issue(s)
 with pytest.raises(VoiceError,match='rate_limit'):issue(s)
def test_revocation_immediate(state):
 s,_,active=state;l=redeem(s,issue(s));active[0]=False
 with pytest.raises(VoiceError,match='revoked'):s.revalidate(l['lease_id'],'adapter')
def test_caps(state):
 s,n,_=state;l=redeem(s,issue(s));s.audio(l['lease_id'],'adapter',20000000)
 with pytest.raises(VoiceError,match='audio_size'):s.audio(l['lease_id'],'adapter',1)
 l=redeem(s,issue(s));n[0]+=600
 with pytest.raises(VoiceError,match='expired'):s.revalidate(l['lease_id'],'adapter')
def test_final_and_recovery(state):
 s,n,_=state;l=redeem(s,issue(s));lease=l['lease_id']
 s.progress(lease,'adapter',{'version':1,'seq':0,'type':'transcript_partial','state':'running','text':'ephemeral'})
 s.progress(lease,'adapter',{'version':1,'seq':1,'type':'transcript_final','state':'running','text':'final component'})
 e={'version':1,'request_id':l['request_id'],'seq':2,'type':'done','state':'done','transcript_text':'final component','answer_text':'answer'}
 assert not s.final(lease,'adapter',e)['duplicate'];assert s.final(lease,'adapter',e)['duplicate']
 with pytest.raises(VoiceError,match='final_conflict'):s.final(lease,'adapter',{**e,'answer_text':'different'})
 with pytest.raises(VoiceError,match='already_final'):s.progress(lease,'adapter',{'version':1,'seq':3,'type':'answer_final','state':'done'})
 r=s.recover(1,'session',l['request_id'],'conversation',s.origin);assert len(r['events'])==2 and 'ephemeral' not in str(r)
 with pytest.raises(VoiceError):s.recover(2,'session',l['request_id'],'conversation',s.origin)
 n[0]+=300
 with pytest.raises(VoiceError,match='expired'):s.recover(1,'session',l['request_id'],'conversation',s.origin)
def test_explicit_revoke_and_wrong_adapter(state):
 s,_,_=state;l=redeem(s,issue(s))
 with pytest.raises(VoiceError):s.revalidate(l['lease_id'],'other')
 s.revoke(1,'session',l['request_id'])
 with pytest.raises(VoiceError,match='revoked'):s.revalidate(l['lease_id'],'adapter')

@pytest.mark.parametrize('reason',['expired','revoked','session_revoked'])
def test_lost_final_retry_never_bypasses_liveness(state,reason):
 s,n,active=state;l=redeem(s,issue(s));lease=l['lease_id']
 e={'version':1,'request_id':l['request_id'],'seq':0,'type':'done','state':'done','answer_text':'committed'}
 s.final(lease,'adapter',e) # pretend response lost
 if reason=='expired':n[0]+=600
 elif reason=='revoked':s.revoke(1,'session',l['request_id'])
 else:active[0]=False
 with pytest.raises(VoiceError):s.final(lease,'adapter',e)
 assert (1,l['request_id']) in s.finals # rejection is NOT evidence of failed commit
 if reason=='revoked':
  assert s.recover(1,'session',l['request_id'],'conversation',s.origin)['events'][-1]['answer_text']=='committed'
 else:
  with pytest.raises(VoiceError):s.recover(1,'session',l['request_id'],'conversation',s.origin)
