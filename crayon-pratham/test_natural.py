import cr_natural as N
import pytest
@pytest.mark.parametrize('text,command',[
 ('turn on voice','/voice on'),('call me Sam','/nickname Sam'),('be more concise','/persona concise'),('quiz me','/play quiz'),('research solar panels in the background','/work brief solar panels'),('set quiet hours to 9pm until 8am','/quiet_hours 21 8'),('show work number 12','/work show 12'),('connect my docs account','connect workspace'),('read aloud Hello there','/speak Hello there')])
def test_plain_routes(text,command):assert N.translate(text)==command
@pytest.mark.parametrize('text',['send this email','delete all files','transfer money','read my inbox','call someone','buy a car'])
def test_no_effect_guess(text):assert N.translate(text)==text

def test_calendar_guest_not_in_request_rejected(monkeypatch):
 import cr_llm as L
 monkeypatch.setattr(L,'ask_json',lambda *a,**k:dict(title='Study',start='2026-10-11T10:00:00+05:30',end='2026-10-11T11:00:00+05:30',timezone='Asia/Calcutta',guests=['invented@example.com'],reminder_minutes=None))
 with pytest.raises(ValueError,match='exact email'):N.calendar_fields('Create a study block tomorrow at10am')
def test_sheet_missing_identity_one_question():
 with pytest.raises(ValueError,match='Which Sheet'):N.sheet_fields('Update my expenses sheet')
def test_sheet_words_to_fields(monkeypatch):
 import cr_llm as L
 monkeypatch.setattr(L,'ask_json',lambda *a,**k:{'area':'Sheet1!A1:B1','values':[['Groceries',200]]})
 assert N.sheet_fields('Update sheet https://docs.google.com/spreadsheets/d/abcdefghijk1234567/edit A1:B1 groceries200')['values']==[['Groceries',200]]

def test_web_preferences_preserve_private_delivery(monkeypatch):
 import cr_channel as C,cr_proactive as P
 from unittest.mock import Mock
 token=C.channel.set('web');out=Mock();captured=[]
 try:
  monkeypatch.setattr(P,'settings',lambda uid:dict(P.DEFAULT))
  monkeypatch.setattr(P,'web_update',lambda uid,f:captured.append(f) or {'text':'private in-app'})
  assert N.handle(1,1,'send morning digests',out)
  assert captured[0]['digest']=='morning' and captured[0]['accept'] is True
 finally:C.channel.reset(token)
