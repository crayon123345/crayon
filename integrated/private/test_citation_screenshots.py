import cr_citations as C
import cr_screenshots as S
import cr_telegram as T

def test_wrong_page_programmes_rejected(monkeypatch):
 monkeypatch.setattr(C.llm,'ask_json',lambda *a,**k:{'claims':[{'claim':'It offers undergraduate programmes','url':'https://site.org/terms-and-conditions','quote':'This site offers undergraduate programmes'}]})
 assert 'do not support' in C.checked_answer('programmes','draft',[{'url':'https://site.org/terms-and-conditions','text':'This site offers undergraduate programmes'}])

def test_quote_must_belong_to_exact_page(monkeypatch):
 monkeypatch.setattr(C.llm,'ask_json',lambda *a,**k:{'claims':[{'claim':'A programme','url':'https://site.org/other','quote':'Actual programme offering description here'}]})
 assert 'do not support' in C.checked_answer('q','draft',[{'url':'https://site.org/course','text':'Actual programme offering description here'},{'url':'https://site.org/other','text':'Other page'}])

def test_supported_claim(monkeypatch):
 monkeypatch.setattr(C.llm,'ask_json',lambda *a,**k:{'claims':[{'claim':'Course offered','url':'https://site.org/course','quote':'Actual programme offering description here'}]})
 assert 'Source: https://site.org/course' in C.checked_answer('q','draft',[{'url':'https://site.org/course','text':'Actual programme offering description here'}])

def test_group_screenshot_no_private_optin(monkeypatch):
 calls=[]
 monkeypatch.setattr(S,'capture',lambda uid,text: calls.append(uid) or ('Webpage screenshot: https://example.com',{'filename':'screen.png','mime':'image/png','data':b'png'}))
 monkeypatch.setattr(T.mem,'user_lock',lambda *a: (_ for _ in ()).throw(AssertionError('No personal model')))
 monkeypatch.setattr(T.db,'kv_get',lambda *a:None)
 out=T.CaptureOut()
 T.handle_update({'message':{'chat':{'id':-991,'type':'supergroup'},'from':{'id':22},'text':'@crayon_v1_bot send screenshots of example.com','message_id':1}},out)
 assert calls==[22] and out.sent[0]['text']=='Attachment: screen.png'

def test_missing_link_and_plural():
 assert S.requested('Send screenshots please')
 assert S.capture(22,'send screenshots')[1] is None
 assert S.url_from('screenshot mastersunion.org')=='https://mastersunion.org'

def test_no_readable_pages_refuses_draft():
 assert 'could not verify' in C.checked_answer('What programmes?', 'Made-up course list', [])

def test_live_fares_no_inventory_no_cheapest_guess():
 text=C.checked_answer('Cheapest flight from Delhi to Kolkata on upcoming days','Flight invented Rs2500',[])
 assert 'current airline fares' in text and 'departure window' in text and '2500' not in text

def test_weather_failure_not_generic_citation_error():
 assert 'current weather reading' in C.checked_answer('Weather in Kolkata today','31C',[])

def test_dynamic_public_lookup_triggers():
 import cr_live as L
 assert L.requested('Cheapest flight from Delhi to Kolkata on any upcoming days')
 assert L.requested('Weather in Kolkata today')
 assert L.requested('Search the web for current news')
 assert not L.requested('Search for my emails')

def test_flight_search_leads_not_quotes(monkeypatch):
 import cr_live as L
 monkeypatch.setattr(L.W,'search',lambda *a:[{'title':'Airline search','url':'https://example.com/flights'}])
 monkeypatch.setattr(L.W,'fetch',lambda *a: (_ for _ in ()).throw(RuntimeError('blocked')))
 r=L.answer('Cheapest flight Delhi Kolkata')
 assert 'cheapest flight' in r and 'What window' in r

def test_regression_snippets_keep_search_links(monkeypatch):
 import cr_live as L
 monkeypatch.setattr(L.W,'search',lambda *a:[{'title':'Forecast source','url':'https://example.com/weather'}])
 monkeypatch.setattr(L.W,'fetch',lambda *a: (_ for _ in ()).throw(RuntimeError('blocked')))
 r=L.answer('weather Kolkata today')
 assert 'readable current data' in r and 'No current number is verified' in r
 assert 'No supported citation' not in r

def test_news_each_headline_source_link(monkeypatch):
 import cr_live as L
 monkeypatch.setattr(L.W,'news',lambda *a:{'items':[{'title':'A','url':'https://news.google.com/a','source':'Publisher','published':'2026-10-10T10:00:00+05:30'},{'title':'B','url':'https://news.google.com/b','source':'Publisher','published':'2026-10-10T11:00:00+05:30'}]})
 r=L.answer('latest news')
 assert 'https://news.google.com/a' in r and 'https://news.google.com/b' in r

def test_agent_search_provider_result_not_silently_discarded(monkeypatch):
 import cr_agent as A,cr_memory as M,cr_tools as T,cr_config as CFG
 monkeypatch.setattr(M,'touch_user',lambda *a:None)
 monkeypatch.setattr(M,'add_message',lambda *a:None)
 monkeypatch.setattr(A,'_history_contents',lambda *a:[])
 monkeypatch.setattr(A,'build_system',lambda *a:'System')
 monkeypatch.setattr(__import__('cr_mcp_user'),'connectors',lambda *a:{})
 monkeypatch.setattr(__import__('cr_reply_context'),'inject',lambda *a:None)
 monkeypatch.setattr(A,'needs_check',lambda *a:False)
 monkeypatch.setattr(A,'honesty_guard',lambda reply,*a:reply)
 outputs=iter([{'model':'fake','calls':[{'name':'web_search','args':{'query':'compiler releases'}}],'parts':[]},{'model':'fake','calls':[],'text':'Unsupported latest version','parts':[]}])
 monkeypatch.setattr(A.llm,'generate',lambda *a,**k:next(outputs))
 calls=[]
 def run(tool,args,ctx):
  calls.append(tool)
  if tool=='web_search':return {'ok':True,'verified':True,'results':[{'title':'Compiler releases','url':'https://example.com/releases'}]}
  return {'ok':False,'verified':False}
 monkeypatch.setattr(T,'run',run)
 reply,meta=A.respond(1000000009999999,1000000009999999,'What changed in compiler releases?',readonly=True)
 assert 'web_search' in calls and 'read_url' in calls
 assert 'readable current data' in reply
 assert 'Unsupported latest version' not in reply

def test_flight_fetched_prices_are_not_bailed_out(monkeypatch):
 import cr_live as L,cr_citations as Cit
 monkeypatch.setattr(L.W,'search',lambda *a:[{'title':'Fares','url':'https://example.com/flights'}])
 monkeypatch.setattr(L.W,'fetch',lambda *a:{'url':'https://example.com/flights','text':'Delhi Kolkata advertised starting fare Rs6200, not dated inventory.'})
 monkeypatch.setattr(L.W.llm,'generate',lambda *a,**k:{'text':'Advertised fare'})
 monkeypatch.setattr(Cit,'checked_answer',lambda *a:'The site advertises Delhi-Kolkata fares from Rs6200, not a dated available quote.\nSource: https://example.com/flights')
 r=L.answer('Cheapest flight Delhi Kolkata')
 assert 'Rs6200' in r and 'Checked:' in r and 'What window' not in r
