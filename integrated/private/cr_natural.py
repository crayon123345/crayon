"""Plain-language front door for deterministic settings and safe internal routes."""
import re,json
class Clarification(ValueError):pass

def translate(text):
    t=text.strip();low=t.lower().rstrip('.!?')
    aliases={'show plugins':'/plugins','what plugins are available':'/plugins','show my connectors':'/mcp','show mcp servers':'/mcp','enable voice':'/voice on','turn on voice':'/voice on','disable voice':'/voice off','turn off voice':'/voice off','show voice settings':'/voice','play a quiz':'/play quiz','quiz me':'/play quiz','play a guessing game':'/play guess','stop the game':'/game_stop','stop playing':'/game_stop','show my tasks':'/tasks','export my tasks':'/tasks export','show my work queue':'/work list','show my memory':'/memory','review my memory':'/memory_review','help':'/help'}
    if low in aliases:return aliases[low]
    proactive={'turn on daily check-ins':'/proactive on','turn off daily check-ins':'/proactive off','send morning digests':'/digest morning','send evening digests':'/digest evening','send morning and evening digests':'/digest both','turn off digests':'/digest off','show my digest':'/digest_now'}
    if low in proactive:return proactive[low]
    m=re.fullmatch(r'(?i)(?:please )?(?:call me|my nickname is) ([\w .-]{1,40})',t)
    if m:return '/nickname '+m[1]
    m=re.fullmatch(r'(?i)(?:be|use a|switch to)(?: more)? (warm|concise|playful|coach)(?: style| tone)?',t)
    if m:return '/persona '+m[1].lower()
    m=re.fullmatch(r'(?i)(?:please )?(?:read|say|speak)(?: aloud)?[: ]+(.+)',t)
    if m and not re.match(r'(?i)(my |the |doc|sheet|email|inbox|calendar)',m[1]):return '/speak '+m[1]
    m=re.fullmatch(r'(?i)(?:please )?(?:connect|link|disconnect|unlink)(?: my)? (workspace|google workspace|docs|sheets|github)(?: account)?',t)
    if m:return ('disconnect' if m[0].lower().startswith(('disconnect','unlink')) else 'connect')+' '+('github' if m[1].lower()=='github' else 'workspace')
    m=re.fullmatch(r'(?i)(?:research|look into|investigate|prepare a brief on) (.+?) (?:in the background|while i am away)',t)
    if m:return '/work brief '+m[1]
    m=re.fullmatch(r'(?i)(?:pause|resume|cancel|show|export)(?: my)? (?:work|job)(?: number| #)? (\d+)',t)
    if m:return '/work '+low.split()[0]+' '+m[1]
    m=re.fullmatch(r'(?i)(?:set|change)(?: my)? quiet hours(?: to)? (\d{1,2})(?:\s*(am|pm))? (?:to|until|through) (\d{1,2})(?:\s*(am|pm))?',t)
    if m:
        def hour(n,part):return int(n)%12+(12 if part and part.lower()=='pm' else 0) if part else int(n)
        a,b=hour(m[1],m[2]),hour(m[3],m[4])
        if 0<=a<=23 and 0<=b<=23:return '/quiet_hours '+str(a)+' '+str(b)
    return text

def handle(uid,chat,text,out):
    import cr_channel
    if cr_channel.channel.get()!='web':return False
    if re.match(r'(?i)(?:(?:show|open)(?: my)? task |mark .* (?:step|task step) (?:in|for|of) )',text.strip()):return __import__('cr_dashboard').handle(uid,chat,text,out)
    command=translate(text)
    if re.fullmatch(r'(?i)(pause|resume|cancel|show|export)(?: my)? (?:work|job|research|brief)(?: on| about| called)? .+',text.strip()):return __import__('cr_work').handle(uid,chat,text,out)
    if command==text:return False
    if command.startswith(('/voice','/speak','/persona','/nickname','/play','/game_stop')):return False
    if chat!=uid:out.send(chat,'This account setting needs your private chat.');return True
    if command.startswith('/work'):
        return __import__('cr_work').handle(uid,chat,command,out)
    if command.startswith('/tasks'):
        return __import__('cr_dashboard').handle(uid,chat,command,out)
    if command in ('/plugins','/mcp'):return __import__('cr_plugins').handle(uid,chat,command,out)
    if command in ('/memory','/memory_review'):
        M=__import__('cr_memory');out.send(chat,M.render_memory(uid) if command=='/memory' else json.dumps(M.review_memory(uid),ensure_ascii=False));return True
    if command=='/digest_now':out.send(chat,__import__('cr_proactive').digest_text(uid));return True
    if command.startswith(('/proactive ','/digest ')):
        op,value=command.split();key='proactive' if op=='/proactive' else 'digest';P=__import__('cr_proactive');settings=P.settings(uid);f={k:settings[k] for k in P.DEFAULT};f[key]=value=='on' if key=='proactive' else value;out.send(chat,P.web_update(uid,{**f,'accept':True})['text']);return True
    if command.startswith('/quiet_hours '):
        P=__import__('cr_proactive');a,b=map(int,command.split()[1:]);settings=P.settings(uid);f={k:settings[k] for k in P.DEFAULT};f.update(quiet_start=a,quiet_end=b,accept=True);out.send(chat,P.web_update(uid,f)['text']);return True
    if re.fullmatch(r'(connect|disconnect) (workspace|github)',command):
        op,provider=command.split();X=__import__('cr_connections')
        if op=='connect':out.send(chat,'Review your own '+provider+' account permissions here: '+X.begin(uid,provider))
        else:X.disconnect(uid,provider);out.send(chat,'Local connector removed. Provider revocation may need checking.')
        return True
    return False

def calendar_fields(text):
    from datetime import datetime
    import cr_llm as L,cr_memory as M
    parsed=L.ask_json(text,system='Parse a calendar creation request. Current local time '+datetime.now().astimezone().isoformat()+'. Return {title,start,end,timezone,guests,reminder_minutes} with ISO times including offset and IANA timezone. No send/create. Only exact emails from user text may be guests. If start date/time, timezone or invitation intent is unclear return {question:missing question}. Solo focus/study blocks have no guests. Do not infer contact email or a meeting with someone means a silent hold. Default duration60minutes only for a clear solo block. For a coordination request end time can default30minutes after exact start. Missing invite address asks. Reminder default null.',default={}) or {}
    if parsed.get('question'):raise Clarification(str(parsed['question'])[:250])
    expected={'title','start','end','timezone','guests','reminder_minutes'}
    if set(parsed)!=expected:raise ValueError('What day, time and timezone should I use, and should anyone be invited?')
    emails=re.findall(r'[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}',text)
    if not isinstance(parsed['guests'],list) or any(e not in emails for e in parsed['guests']):raise ValueError('Who should receive an invitation? Give their exact email.')
    return parsed

def sheet_fields(text):
    import cr_llm as L
    link=re.search(r'https://docs\.google\.com/spreadsheets/d/([A-Za-z0-9_-]{15,150})',text)
    if not link:raise Clarification('Which Sheet should I update? Send its link once. File-name discovery needs Drive access, which is not connected by the current Workspace scope.')
    parsed=L.ask_json(text,system='Extract only the requested Sheet cell changes from this text. Return {area:"Sheet1!A1:B2",values:[["text",1]]}. Up to20rows/10columns. RAW only, no formulas. Do not read any Sheet or infer existing values, expenses, cells or ranges. If requested cells or values are missing return {question:missing question}. No actions.',default={}) or {}
    if parsed.get('question'):raise Clarification(str(parsed['question'])[:250])
    if set(parsed)!={'area','values'}:raise ValueError('What cells and values should change?')
    return {'sid':link[1],'area':parsed['area'],'values':parsed['values']}
