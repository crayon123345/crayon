"""Private in-app inbox. No phone/email/push destination inferred."""
import hashlib,json
import cr_db as db

def init():
    db.q('''CREATE TABLE IF NOT EXISTS web_notifications(
      id TEXT PRIMARY KEY,user_id BIGINT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,encrypted TEXT NOT NULL,
      created_at TIMESTAMPTZ DEFAULT now(),seen BOOLEAN DEFAULT false)''',fetch='none')

def publish(uid,text,key):
    import cr_web_app as W
    init()
    if not db.q('SELECT user_id FROM users WHERE user_id=%s',(uid,),'one'):raise ValueError('Account removed')
    ident=hashlib.sha256((str(uid)+':'+key).encode()).hexdigest()
    db.q('INSERT INTO web_notifications(id,user_id,encrypted) SELECT %s,user_id,%s FROM users WHERE user_id=%s ON CONFLICT DO NOTHING',(ident,W.encode({'text':text}),uid),'none')
    db.q("DELETE FROM web_notifications WHERE user_id=%s AND id NOT IN (SELECT id FROM web_notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 100)",(uid,uid),'none')
    return ident

def list_for(uid):
    import cr_web_app as W
    init()
    rows=db.q('SELECT id,encrypted,created_at,seen FROM web_notifications WHERE user_id=%s ORDER BY created_at DESC LIMIT 30',(uid,))
    return [{'id':r['id'],'text':W.decode(r['encrypted'])['text'],'created_at':str(r['created_at']),'seen':r['seen']} for r in rows]

def acknowledge(uid,ident):
    if not isinstance(ident,str) or not __import__('re').fullmatch('[a-f0-9]{64}',ident):raise ValueError('Invalid notification')
    init();db.q('UPDATE web_notifications SET seen=true WHERE id=%s AND user_id=%s',(ident,uid),'none')
