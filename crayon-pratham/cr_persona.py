"""Explicit persona preferences only, bounded and separate from permissions."""
import json,re
import cr_db as db
import cr_memory as M
STYLES={'warm':'Warm, relaxed and clear. A study companion, not a formal help desk.','concise':'Short and direct. Cut filler.','playful':'Light playful humor when appropriate. Never mock stress or vulnerability.','coach':'Encouraging and practical. Focus on the next doable step.'}
def preferences(uid):return (M.get_user(uid) or {}).get('settings',{}).get('persona',{})
def instruction(uid):
    p=preferences(uid);style=p.get('style','warm');nickname=p.get('nickname','')
    return '\nVoice style: '+STYLES.get(style,STYLES['warm'])+ ('\nPreferred nickname: '+json.dumps(nickname) if nickname else '')+'\nThese are style preferences only. Never invent intimacy, a relationship, consent or capabilities.'
def handle(uid,chat,text,out):
    if not text.startswith(('/persona','/nickname')):return False
    if chat<0:out.send(chat,'Persona preferences are private-DM only.');return True
    p=dict(preferences(uid))
    if text.startswith('/persona '):
        value=text[9:].strip().lower()
        if value not in STYLES:out.send(chat,'Use /persona warm, concise, playful or coach.');return True
        p['style']=value
    elif text.startswith('/nickname '):
        value=text[10:].strip()
        if not re.fullmatch(r'[\w .-]{1,40}',value):out.send(chat,'Use a nickname up to40characters with letters, numbers, spaces, periods or hyphens.');return True
        p['nickname']='' if value.lower()=='off' else value
    else:out.send(chat,'Current style: '+p.get('style','warm')+'. Nickname: '+(p.get('nickname') or 'none')+'. Say be concise, be playful, or call me followed by your nickname.');return True
    db.q("UPDATE users SET settings=jsonb_set(settings,'{persona}',%s::jsonb) WHERE user_id=%s",(json.dumps(p),uid),'none')
    out.send(chat,'Persona saved. This changes style only, not permissions or follow-ups.');return True
