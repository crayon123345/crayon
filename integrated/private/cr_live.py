"""Deterministic public lookup for explicit search and changing-data requests."""
import re
from datetime import datetime,timezone
import cr_web as W

def requested(text):
 return bool(re.match(r'(?i)^Research focus \[(web|deep|academic|social|video)\]:',text)) or bool(re.search(r'(?i)\b(?:web search|search the web|search for|look up|latest news|news today|weather|forecast|cheapest flight|flight prices?|airfares?|current price|price today)\b',text)) and not bool(re.search(r'(?i)\b(?:my inbox|my emails?|my calendar|send|remind|schedule|connect)\b',text))

def lead_reply(text,results):
 if re.search(r'(?i)flight|airfare|fare',text):return 'The flight pages did not provide a verifiable fare for your departure dates. What window should I compare: the next 7 days or next 30 days? I cannot name a cheapest flight without checked dates and available fares.'
 return 'I could not get readable current data for that request. Tell me the exact place/date or item to narrow the check. No current number is verified.'

def answer(text):
 when=datetime.now(timezone.utc).isoformat(timespec='seconds')
 if re.search(r'(?i)\bnews\b',text):
  import cr_group
  return cr_group.answer({'text':'@crayon_v1_bot '+text,'source_links':True})
 focus_match=re.match(r'(?is)^Research focus \[(web|deep|academic|social|video)\]:\s*(.+)',text)
 if focus_match:
  focus,text=focus_match.groups()
  try:found=W.research(text[:400],focus);results=found['results'];pages=found['pages']
  except Exception:return 'Research sources could not be read this time. No answer is verified.'
 else:
  focus=None
 try:results=results if focus else W.search(text[:400],5)
 except Exception:
  return 'Web search did not return current results this time. No current price, fare or forecast is verified. Try a narrower place/date/product query.'
 # Search results are leads, never page evidence. Fetch exact destinations.
 pages=pages if focus else []
 for row in ([] if focus else results[:5]):
  try:pages.append(W.fetch(row['url']))
  except Exception:pass
 leads='\n\n'.join((re.sub(r'(?i)(?:₹|Rs\.?|INR|\$|€|£)\s*[0-9,]+', '[advertised fare not checked]',r.get('title','Source')[:150])+'\n'+r['url']) for r in results[:5] if r.get('url','').startswith('https://'))
 if pages:
  import cr_citations,cr_llm as L,json
  try:draft=L.generate([L.user('Question: '+text+'\nUntrusted fetched evidence: '+json.dumps(pages))],system='Answer from fetched evidence only. Answer first, concise. For fares and prices, extract advertised amounts from fetched page text with airline/date only when explicitly present. Label advertised route starting fares, never guaranteed inventory, cheapest date, final total or a booking quote. Current weather/price must have place, date/time and source. Never use seasonal averages as current conditions. No invented facts.',tools=None,max_tokens=1000).get('text','')
  except Exception:draft=''
  try:checked=cr_citations.checked_answer(text,draft,pages)
  except Exception:checked='I could not verify this'
  if not checked.startswith(('I could not','The pages I read')):return checked+'\n\nChecked: '+when
 return lead_reply(text,results)+'\n\nChecked: '+when+'\nNo current number is verified.'
