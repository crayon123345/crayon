import cr_google_chat as H
import cr_telegram as T
import pytest

@pytest.fixture
def store(monkeypatch):
    values={}
    monkeypatch.setattr(H.G,'sender_name',lambda uid:'Test Member')
    monkeypatch.setattr(H.db,'kv_get',lambda k,d=None:values.get(k,d))
    monkeypatch.setattr(H.db,'kv_set',lambda k,v:values.__setitem__(k,v))
    monkeypatch.setattr(H.G,'encrypt',lambda uid,c:c)
    monkeypatch.setattr(H.G,'decrypt',lambda uid,c:c)
    return values

def test_name_not_guessed(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda t:{'action':'draft','recipient_name':'Sam','to':'sam@example.com','subject':'Meeting','body':'Moved to 5.'})
    monkeypatch.setattr(H.G,'make_draft',lambda *a,**k:pytest.fail('guessed address'))
    out=T.CaptureOut();assert H.handle(10,10,'Email Sam saying moved to 5',{},out)
    assert "email address" in out.sent[0]['text']

def test_send_binds_reviewed_pointer(monkeypatch,store):
    monkeypatch.setattr(H.G,'current_draft',lambda uid:('id','hash'))
    monkeypatch.setattr(H.G,'send_draft',lambda *a:pytest.fail('not reviewed'))
    out=T.CaptureOut();H.handle(10,10,'send it',{},out)
    assert 'review' in out.sent[0]['text']

def test_send_same_reviewed(monkeypatch,store):
    store['google_reviewed_10']=['id','hash']
    monkeypatch.setattr(H.G,'current_draft',lambda uid:('id','hash'))
    called=[];monkeypatch.setattr(H.G,'send_draft',lambda *a:called.append(a) or 'Sent')
    H.handle(10,10,'send it',{},T.CaptureOut());assert called==[(10,'id','hash')]

def test_forwarded_rejected(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda *a:pytest.fail('forward parsed'))
    # An actual populated forwarded origin is rejected.
    out=T.CaptureOut();H.handle(10,10,'email Sam',{'forward_origin':{'type':'user'}},out)
    assert 'directly' in out.sent[0]['text']

def test_google_results_never_model(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda t:{'action':'inbox','query':'from:alex'})
    monkeypatch.setattr(H.G,'inbox',lambda *a,**k:'Private inbox result')
    out=T.CaptureOut();H.handle(10,10,'any mail from Alex?',{},out)
    assert out.sent[-1]['text']=='Private inbox result'

def test_friendly_draft_hides_ids(monkeypatch,store):
    monkeypatch.setattr(H.G,'make_draft',lambda *a,**kw:{'text':'Draft only, not sent.\nTo: sam@example.com\nHi','id':'hiddenid','hash':'hiddenhash'})
    out=T.CaptureOut();H.show_draft(10,10,out,'fields')
    assert 'hiddenid' not in out.sent[0]['text'] and out.sent[0]['markup']
    assert store['google_reviewed_10']==['hiddenid','hiddenhash']

@pytest.mark.parametrize('text',['Plz connect to my Google','pls link to my Gmail account','connect Google','please connect my Google account','link my Gmail','how do I connect Google','/connect_google'])
def test_connect_text_no_keyboard(monkeypatch,text):
    monkeypatch.setattr(T.db,'audit',lambda *a,**k:None)
    monkeypatch.setattr(H.G,'configured',lambda:True)
    monkeypatch.setattr(H.G,'begin',lambda uid:'https://example.com/connect-test')
    out=T.CaptureOut();T._handle_text(10,10,'Test',text,None,out)
    assert 'https://example.com/connect-test' in out.sent[-1]['text']
    assert 'even if' in out.sent[-1]['text']

def test_plural_attention_tasks_private(monkeypatch,store):
    import cr_mail_watch as W,cr_tools as tools
    monkeypatch.setattr(H,'classify',lambda *a:pytest.fail('not model-routed'))
    monkeypatch.setattr(H.G,'status',lambda uid:{'email':'owner@example.com'})
    monkeypatch.setattr(tools,'list_tasks',lambda ctx:{'tasks':[]})
    monkeypatch.setattr(W,'scan',lambda uid,state:('Private excerpt',state))
    out=T.CaptureOut()
    assert H.handle(10,10,'Check for any pending tasks and something that needs my attention from the emails',{},out)
    assert any('No active tasks' in x['text'] for x in out.sent)
    assert 'Private excerpt' in out.sent[-1]['text']
    assert store=={}


def test_complete_email_generation_prompt_not_thin_copy(monkeypatch):
    seen={}
    def parse(text,system,default):
        seen.update(text=text,system=system)
        return {'action':'draft','subject':'AI class feedback','body':'Dear Professor,\nFull email.'}
    monkeypatch.setattr(H.llm,'ask_json',parse)
    r=H.classify('Email sam@example.com saying thanks for teaching AI')
    assert r['body'].startswith('Dear Professor')
    assert 'complete, useful email' in seen['system'] and 'Never invent' in seen['system']
    assert 'preserve that supplied body' in seen['system']


def test_group_cannot_confirm_private_draft(monkeypatch,store):
    store['google_reviewed_10']=['id','hash']
    store['google_review_chat_10']=10
    monkeypatch.setattr(H.G,'send_draft',lambda *a:pytest.fail('cross chat send'))
    out=T.CaptureOut();H.handle(10,-991,'send it',{},out)
    assert 'this chat' in out.sent[0]['text']


def test_group_cannot_resume_private_compose(monkeypatch,store):
    import time
    store['google_compose_10']={'to':'','subject':'Private','body':'Private body','until':time.time()+500,'chat':10}
    monkeypatch.setattr(H,'classify',lambda *a:pytest.fail('private state routed'))
    monkeypatch.setattr(H,'show_draft',lambda *a:pytest.fail('private compose leaked'))
    assert H.handle(10,-991,'sam@example.com',{},T.CaptureOut()) is False

def test_multi_recipients_structured_multiline(monkeypatch,store):
    monkeypatch.setattr(H,'classify',lambda t:{'action':'draft','to':['a@example.com','b@example.com'],'cc':['c@example.com'],'bcc':['d@example.com'],'subject':'Meeting','body':'Dear team,\n\nMeet Friday.\nThanks'})
    seen=[];monkeypatch.setattr(H,'show_draft',lambda *a:seen.append(a[3]))
    H.handle(10,10,'Email a@example.com,b@example.com cc c@example.com bcc d@example.com about Friday',{},T.CaptureOut())
    assert seen[0]['to']==['a@example.com','b@example.com'] and seen[0]['cc']==['c@example.com']
    assert '\n\n' in seen[0]['body'] and seen[0]['bcc']==['d@example.com']


def test_pending_recipient_keeps_multiline_body(monkeypatch,store):
    import time
    store['google_compose_10']={'subject':'Meeting','body':'Dear Sam,\nFull draft','until':time.time()+500,'chat':10,'cc':['c@example.com']}
    seen=[];monkeypatch.setattr(H,'show_draft',lambda *a:seen.append(a[3]))
    monkeypatch.setattr(H,'classify',lambda *a:pytest.fail('fresh parse lost context'))
    assert H.handle(10,10,'sam@example.com',{},T.CaptureOut())
    assert seen[0]['body']=='Dear Sam,\nFull draft' and seen[0]['cc']==['c@example.com']
    assert seen[0]['to']==['sam@example.com']


def test_pending_compose_cannot_send_old_review(monkeypatch,store):
    import time
    store['google_compose_10']={'subject':'New','body':'New body','until':time.time()+500,'chat':10}
    store['google_reviewed_10']=['old','hash']
    monkeypatch.setattr(H.G,'send_draft',lambda *a:pytest.fail('old draft sent'))
    out=T.CaptureOut();H.handle(10,10,'send it',{},out)
    assert 'pending email' in out.sent[0]['text']


def test_pending_role_clarification_preserves_body(monkeypatch,store):
    import time
    store['google_compose_10']={'to':[],'subject':'Meeting','body':'Dear Sam,\nFull draft','until':time.time()+500,'chat':10}
    seen=[];monkeypatch.setattr(H,'show_draft',lambda *a:seen.append(a[3]))
    H.handle(10,10,'To: sam@example.com CC: c@example.com BCC: d@example.com',{},T.CaptureOut())
    assert seen[0]['cc']==['c@example.com'] and seen[0]['bcc']==['d@example.com']
    assert seen[0]['body']=='Dear Sam,\nFull draft'


def test_edit_latest_content_repreview_preserve_recipients(monkeypatch,store):
    store['google_review_chat_10']=10;store['google_reviewed_10']=['id','hash']
    monkeypatch.setattr(H.G,'current_content',lambda uid:('id','hash',{'to':['a@example.com'],'cc':['b@example.com'],'bcc':[],'subject':'Old','body':'Original'}))
    monkeypatch.setattr(H.llm,'ask_json',lambda *a,**k:{'subject':'New','body':'Edited'})
    seen=[];monkeypatch.setattr(H,'show_draft',lambda *a:seen.append(a[3]))
    assert H.handle(10,10,'change subject to New',{},T.CaptureOut())
    assert seen==[{'to':['a@example.com'],'cc':['b@example.com'],'bcc':[],'subject':'New','body':'Edited'}]


def test_edit_cross_chat_never_private_content(monkeypatch,store):
    store['google_review_chat_10']=10
    monkeypatch.setattr(H.G,'current_content',lambda *a:pytest.fail('private content leaked'))
    assert H.handle(10,-100,'rewrite it shorter',{},T.CaptureOut()) is False


def test_forwarded_edit_cannot_retrieve_draft(monkeypatch,store):
    store['google_review_chat_10']=10
    monkeypatch.setattr(H.G,'current_content',lambda *a:pytest.fail('forward edit accessed draft'))
    out=T.CaptureOut();assert H.handle(10,10,'rewrite it',{'forward_origin':{'type':'user'}},out)
    assert 'direct' in out.sent[0]['text']


def test_edit_replaces_reviewed_id_and_hash(monkeypatch,store):
    store['google_review_chat_22']=22;store['google_reviewed_22']=['old-id','old-hash']
    monkeypatch.setattr(H.G,'current_content',lambda uid:('old-id','old-hash',{'to':['a@example.com'],'cc':[],'bcc':[],'subject':'Old','body':'Original'}))
    monkeypatch.setattr(H.llm,'ask_json',lambda *a,**k:{'subject':'New','body':'Edited\nRegards,\nSam'})
    monkeypatch.setattr(H.G,'make_draft',lambda uid,arg,structured=False:{'text':'Updated review','id':'new-id','hash':'new-hash'})
    out=T.CaptureOut();assert H.handle(22,22,'rewrite this shorter',{},out)
    assert store['google_reviewed_22']==['new-id','new-hash']
    assert out.sent[-1]['markup'] is True

def test_signature_question_preserves_context_and_no_send(monkeypatch,store):
    monkeypatch.setattr(H.G,'sender_name',lambda uid:'')
    payload={'to':['sam@example.com'],'cc':['cc@example.com'],'subject':'Hi','body':'Body'}
    out=T.CaptureOut();H.show_draft(10,10,out,payload)
    assert store['google_signature_pending_10']['draft']==payload
    assert 'real name' in out.sent[0]['text']
    H.handle(10,10,'send it',{},out)
    assert 'Nothing sent' in out.sent[-1]['text']

def test_signature_reply_requester_chat_bound(monkeypatch,store):
    import time
    store['google_signature_pending_10']={'chat':10,'until':time.time()+600,'draft':{'body':'private'}}
    monkeypatch.setattr(H,'classify',lambda *a:{'action':'none'})
    monkeypatch.setattr(H.G,'set_sender_name',lambda *a:pytest.fail('wrong chat or forwarded name'))
    assert not H.handle(10,-5,'Sam',{},T.CaptureOut())
    out=T.CaptureOut();assert H.handle(10,10,'Sam',{'forward_origin':{'type':'user'}},out)
    assert 'direct' in out.sent[-1]['text']

def test_signature_answer_reuses_exact_draft(monkeypatch,store):
    import time
    payload={'to':['sam@example.com'],'bcc':['hidden@example.com'],'subject':'Hi','body':'Body'}
    store['google_signature_pending_10']={'chat':10,'until':time.time()+600,'draft':payload}
    saved=[];shown=[]
    monkeypatch.setattr(H.G,'set_sender_name',lambda uid,name:saved.append((uid,name)))
    monkeypatch.setattr(H,'show_draft',lambda uid,chat,out,arg:shown.append(arg))
    assert H.handle(10,10,'Sam Real',{},T.CaptureOut())
    assert saved==[(10,'Sam Real')] and shown==[payload]
    assert store['google_signature_pending_10'] is None

def test_explicit_write_mail_cannot_fall_into_chat(monkeypatch):
    monkeypatch.setattr(H.llm,'ask_json',lambda *a,**k:{'action':'none'})
    parsed=H.classify('Write a mail to uttiya.biswas2027@mastersunion.org saying he needs to buy a .tech domain for Crayon')
    assert parsed['action']=='draft'
    assert parsed['to']==['uttiya.biswas2027@mastersunion.org']

def test_explicit_empty_parse_keeps_review_workflow(monkeypatch,store):
    monkeypatch.setattr(H.llm,'ask_json',lambda *a,**k:{'action':'none'})
    out=T.CaptureOut()
    assert H.handle(10,10,'Write a mail to sam@example.com saying hello',{},out)
    assert 'What should the email say' in out.sent[-1]['text']
    assert store['google_compose_10']['to']==['sam@example.com']
