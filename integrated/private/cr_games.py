"""Small deterministic games scoped to requester and chat; no model or private memory."""
import secrets,time
import cr_db as db
QUIZZES=[('Which planet is known as the Red Planet?',['Venus','Mars','Jupiter','Mercury'],1),('What is 12 x 8?',['84','88','96','108'],2),('Which is a prime number?',['21','27','29','33'],2),('Water freezes at what temperature in Celsius?',['0','10','32','100'],0)]
def handle(uid,chat,text,out):
    key='game:'+str(uid)+':'+str(chat)
    raw_candidate=text.strip().lower().rstrip('.!')
    candidates={o.lower() for q,options,a in QUIZZES for o in options}
    state=(db.kv_get(key) or {}) if raw_candidate.isdigit() or raw_candidate in candidates else {}
    if state.get('expires',0)>time.time() and not text.startswith('/'):
        raw=text.strip().lower().rstrip('.!')
        if raw.isdigit():text=('/answer ' if state.get('type')=='quiz' else '/guess ')+raw
        elif state.get('type')=='quiz':
            options=QUIZZES[state['index']][1]
            for n,option in enumerate(options,1):
                if raw==option.lower():text='/answer '+str(n);break
    bits=text.strip().split();cmd=bits[0].lower() if bits else ''
    if cmd not in ('/play','/quiz','/guess','/answer','/game_stop'):return False
    key='game:'+str(uid)+':'+str(chat)
    if cmd=='/game_stop':db.kv_set(key,None);out.send(chat,'Game stopped.');return True
    if cmd in ('/play','/quiz'):
        mode=bits[1].lower() if len(bits)>1 else 'quiz'
        if mode=='guess':
            db.kv_set(key,{'type':'guess','number':secrets.randbelow(20)+1,'tries':0,'expires':time.time()+1800});out.send(chat,'I picked a number from 1 to 20. You have 5 tries. Reply with your guess.');return True
        if mode!='quiz':out.send(chat,'Try /play quiz or /play guess. Your game is separate from other members.');return True
        i=secrets.randbelow(len(QUIZZES));q,options,answer=QUIZZES[i]
        db.kv_set(key,{'type':'quiz','index':i,'expires':time.time()+1800})
        out.send(chat,q+'\n'+'\n'.join(str(n+1)+'. '+x for n,x in enumerate(options))+'\nReply with an option or its number.');return True
    state=db.kv_get(key) or {}
    if state.get('expires',0)<time.time():out.send(chat,'No active game. Say quiz me or play a guessing game.');return True
    try:n=int(bits[1])
    except Exception:out.send(chat,'Use '+cmd+' NUMBER.');return True
    if cmd=='/answer' and state.get('type')=='quiz':
        if n not in (1,2,3,4):out.send(chat,'Pick 1, 2, 3 or 4.');return True
        q,opts,a=QUIZZES[state['index']];db.kv_set(key,None);out.send(chat,('Correct! ' if n==a+1 else 'Not quite. ')+'The answer is '+opts[a]+'. Say quiz me to play again.');return True
    if cmd=='/guess' and state.get('type')=='guess':
        if not 1<=n<=20:out.send(chat,'Pick a number from 1 to 20.');return True
        state['tries']+=1
        if n==state['number'] or state['tries']>=5:
            db.kv_set(key,None);out.send(chat,('You got it!' if n==state['number'] else 'Out of tries. It was '+str(state['number'])+'.')+' Say play a guessing game to start again.');return True
        db.kv_set(key,state);out.send(chat,('Higher.' if n<state['number'] else 'Lower.')+' '+str(5-state['tries'])+' tries left.');return True
    out.send(chat,'That command does not match your game. Use /game_stop to reset.');return True
