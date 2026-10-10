"""Display-only command receipts. Never part of model memory or private Google body retention."""
import json,secrets
import cr_db as db
import cr_web_auth as A

def init():
    db.q('''CREATE TABLE IF NOT EXISTS channel_history(id TEXT PRIMARY KEY,user_id BIGINT NOT NULL,
        encrypted TEXT NOT NULL,ts TIMESTAMPTZ DEFAULT now())''',fetch='none')

class ReceiptOut:
    def __init__(self,out,uid,private):self.out,self.uid,self.private,self.lines=out,uid,private,[]
    def __getattr__(self,name):return getattr(self.out,name)
    def send(self,chat,text,markup=None):
        result=self.out.send(chat,text,markup)
        if not self.private:self.lines.append(str(text)[:16000])
        return result

def run(uid,chat,name,text,message_id,out,handler):
    # Groups and secret-looking inputs keep existing behavior and audience restrictions.
    from cr_safety import looks_like_secret
    if chat!=uid or looks_like_secret(text):return handler(uid,chat,name,text,message_id,out)
    before=db.q('SELECT max(id) AS n FROM messages WHERE user_id=%s',(uid,),'one')['n']
    private=True # Command continuations may omit provider words; retain generic receipts only.
    wrapped=ReceiptOut(out,uid,private)
    result=handler(uid,chat,name,text,message_id,wrapped)
    after=db.q('SELECT max(id) AS n FROM messages WHERE user_id=%s',(uid,),'one')['n']
    if after==before and (wrapped.lines or private):
        init()
        # Private-provider bodies and drafted email text are deliberately not copied into display history.
        data={'user':'[Private account command; exact contents not retained here]' if private else text[:8000],
              'assistant':'[Private account response was shown in Telegram. Not retained in chat history or sent to AI.]' if private else '\n\n'.join(wrapped.lines)[:20000]}
        db.q('INSERT INTO channel_history(id,user_id,encrypted) VALUES(%s,%s,%s) ON CONFLICT DO NOTHING',
             ('tg_'+str(message_id) if message_id else secrets.token_hex(16),uid,A._cipher().encrypt(json.dumps(data).encode()).decode()),'none')
    return result
